"""The MCP work folder: nothing is read or written outside it."""
import os
from pathlib import Path

import pytest

from app.workdir import FileRefused, WorkDir


@pytest.fixture
def root(tmp_path):
    folder = tmp_path / 'work'
    folder.mkdir()
    (folder / 'fotos').mkdir()
    (folder / 'fotos' / 'frente.jpg').write_bytes(b'\xff\xd8\xff jpeg')
    (tmp_path / 'fora.jpg').write_bytes(b'\xff\xd8\xff outside')
    return folder


def code(call):
    with pytest.raises(FileRefused) as caught:
        call()
    return caught.value.code


def test_relative_and_absolute_paths_inside_are_read(root):
    work = WorkDir(root)
    assert work.read_image('fotos/frente.jpg').startswith(b'\xff\xd8')
    assert work.read_image(str(root / 'fotos' / 'frente.jpg')).startswith(b'\xff\xd8')


def test_dot_dot_is_refused_even_when_it_comes_back_inside(root):
    work = WorkDir(root)
    assert code(lambda: work.read_image('../fora.jpg')) == 'PATH_OUTSIDE_WORKDIR'
    assert code(lambda: work.read_image('fotos/../fotos/frente.jpg')) == 'PATH_OUTSIDE_WORKDIR'


def test_absolute_path_outside_is_refused(root):
    outside = str(root.parent / 'fora.jpg')
    assert code(lambda: WorkDir(root).read_image(outside)) == 'PATH_OUTSIDE_WORKDIR'


def test_link_that_leads_out_is_refused(root):
    link = root / 'atalho.jpg'
    try:
        os.symlink(root.parent / 'fora.jpg', link)
    except (OSError, NotImplementedError):
        pytest.skip('this system does not allow creating symbolic links')
    assert code(lambda: WorkDir(root).read_image('atalho.jpg')) == 'PATH_OUTSIDE_WORKDIR'


def test_env_variable_sets_the_folder(root, monkeypatch):
    monkeypatch.setenv('MLAM_WORKDIR', str(root))
    assert WorkDir().root == Path(os.path.realpath(root))


def test_large_empty_missing_and_wrong_extension(root, monkeypatch):
    work = WorkDir(root)
    monkeypatch.setattr('app.workdir.MAX_IMAGE', 4)
    assert code(lambda: work.check_image('fotos/frente.jpg')) == 'FILE_TOO_LARGE'
    (root / 'vazia.png').write_bytes(b'')
    assert code(lambda: work.check_image('vazia.png')) == 'EMPTY_FILE'
    assert code(lambda: work.check_image('nao-existe.png')) == 'FILE_NOT_FOUND'
    (root / 'foto.gif').write_bytes(b'GIF89a')
    assert code(lambda: work.check_image('foto.gif')) == 'INVALID_MIME'


def test_template_limit(root, monkeypatch):
    (root / 'modelo.xlsx').write_bytes(b'x' * 10)
    monkeypatch.setattr('app.workdir.MAX_TEMPLATE', 5)
    assert code(lambda: WorkDir(root).read_template('modelo.xlsx')) == 'FILE_TOO_LARGE'


def test_output_needs_xlsx_and_overwrite(root):
    work = WorkDir(root)
    assert code(lambda: work.output('saida.csv', False)) == 'INVALID_OUTPUT'
    assert code(lambda: work.output('../saida.xlsx', False)) == 'PATH_OUTSIDE_WORKDIR'
    assert code(lambda: work.output('sem-pasta/saida.xlsx', False)) == 'FILE_NOT_FOUND'
    target = work.output('saida.xlsx', False)
    work.write(target, b'one')
    assert code(lambda: work.output('saida.xlsx', False)) == 'OUTPUT_EXISTS'
    work.write(work.output('saida.xlsx', True), b'two')
    assert (root / 'saida.xlsx').read_bytes() == b'two'
    assert [p.name for p in root.iterdir() if p.name.startswith('.mlam-')] == []

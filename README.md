# 📦 Mercado Livre - Anúncios em Massa (Gerador de Planilhas Excel)

[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-009688.svg?style=flat&logo=fastapi)](https://fastapi.tiangolo.com)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Open Source](https://img.shields.io/badge/Open%20Source-%E2%99%A5-red.svg)](https://github.com/nogran/mercado-livre-anuncios-em-massa)

> [!NOTE]
> **Aviso de Projeto Independente:** Este é um software comunitário de código aberto mantido por desenvolvedores independentes. Não é uma ferramenta oficial e não possui qualquer afiliação, endosso ou vínculo com o Mercado Livre ou com a MercadoLibre Inc.
>
> *English summary:* Decoupled, stateless open-source engine and FastAPI server to generate, populate, and validate official Mercado Libre bulk listing Excel spreadsheets (`.xlsx`).

Ferramenta desacoplada e API FastAPI para **preenchimento automático da planilha oficial do Anunciador em Massa do Mercado Livre**.

---

## 🎯 Por que este projeto existe?

O Mercado Livre disponibiliza a funcionalidade oficial de **"Anunciar em Massa"** (ou **"Publicar em Massa"**), na qual o vendedor faz o download de um modelo de planilha Excel (`.xlsx`) com dezenas de colunas, regras estritas, abas auxiliares e validações.

Preencher manualmente centenas de produtos, variações (cores, tamanhos, voltagens), guias de medidas, códigos EAN/GTIN e URLs de imagens é:
1. **Lento e exaustivo**, exigindo horas de digitação manual;
2. **Altamente propenso a falhas**, pois qualquer fórmula quebrada, aba renomeada ou formatação incorreta (como EAN que perde os zeros à esquerda) faz o Mercado Livre rejeitar todo o lote.

**Nossa solução:** Você fornece os dados dos seus produtos (via JSON, formulário ou chamada de API) junto com o Excel vazio baixado do Mercado Livre. O sistema faz a inserção cirúrgica dos dados, preserva 100% da integridade da planilha oficial e **devolve o arquivo Excel pronto para você fazer o upload manual no Mercado Livre.**

---

## ✨ Principais Recursos

- **Preenchimento Cirúrgico em XML (OpenXML):** Manipula o `.xlsx` internamente via stream ZIP/XML, sem o risco de corromper abas ocultas (`extra info`, `Ajuda`), fórmulas de cálculo automático ou listas de validação de dados.
- **Formatação Confiável de EAN/GTIN:** Utiliza armazenamento literal (`inlineStr`), garantindo que códigos de barras numéricos não percam os zeros à esquerda e nem virem notação científica (ex: `7.89E+12`).
- **Upload Automatizado de Imagens:** Módulo de integração com a API de Fotos do Mercado Livre (`/pictures/items/upload`) para subir fotos locais e gerar URLs HTTPS seguras no formato exato que a planilha exige.
- **Validação Pré-Upload:** Inspeciona limites de caracteres de títulos (máximo de 60 caracteres), preços mínimos e dados obrigatórios antes de você enviar a planilha para o Mercado Livre.
- **API FastAPI e Interface Swagger:** Endpoints leves e sem estado (stateless) com documentação interativa em `/docs`, facilitando a integração com ERPs (Bling, Tiny), scripts próprios ou e-commerces.

---

## 🗺️ Roadmap de Categorias

- [x] **Fase 1 — Camisetas e Regatas (Vestuário):**
  - Grade de tamanhos e variações de cores na mesma família de anúncio.
  - Vínculo com Guia de Tamanhos oficial (aba 4).
  - Validação estrita de colunas e atributos de moda.
- [ ] **Fase 2 — Sistema Dinâmico de Perfis de Categoria:**
  - Descoberta automática de colunas, cabeçalhos e listas de opções de qualquer planilha baixada do Mercado Livre.
  - Suporte agnóstico a categorias como *Calçados*, *Eletrônicos*, *Casa & Decoração*, *Acessórios Automotivos*, etc.
- [ ] **Fase 3 — CLI (Interface de Linha de Comando):**
  - Comando de terminal (`typer`/`click`) para preenchimento de lotes direto em pipelines CI/CD ou scripts bash/powershell.
- [ ] **Fase 4 — Publicação Direta via API (`POST /items`):**
  - Módulo opcional para quem deseja publicar direto na conta do vendedor sem passar pela etapa de upload manual da planilha.

---

## 🔄 Como Funciona o Fluxo de Trabalho

```mermaid
flowchart LR
    A["1. Baixe o Excel vazio<br/>no Mercado Livre"] --> B["2. Envie o Excel + Dados<br/>para a API /fill"]
    B --> C["3. Baixe o Excel<br/>100% preenchido"]
    C --> D["4. Upload manual na página<br/>do Mercado Livre"]
```

1. **Baixar o Modelo:** No painel do vendedor do Mercado Livre, vá em **Anúncios > Anunciar em massa** e baixe o modelo vazio da categoria desejada.
2. **Preencher via API:** Envie o arquivo `.xlsx` e os dados dos seus produtos para o endpoint `/v1/workbooks/fill`.
3. **Receber o Arquivo:** A API processa e devolve a planilha pronta para download imediato.
4. **Publicar:** Acesse a tela de envio do Mercado Livre e faça o upload da planilha preenchida.

---

## 🚀 Instalação e Execução Local

### Pré-requisitos
- Python 3.11 ou superior
- Gerenciador de pacotes `pip`

### 1. Clonar o repositório
```bash
git clone https://github.com/nogran/mercado-livre-anuncios-em-massa.git
cd mercado-livre-anuncios-em-massa
```

### 2. Criar e ativar o ambiente virtual
```bash
# Linux / macOS
python3 -m venv .venv
source .venv/bin/activate

# Windows (PowerShell)
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 3. Instalar as dependências
```bash
pip install -r requirements.txt
```

### 4. Iniciar a API FastAPI
```bash
uvicorn app.server:app --reload --port 8000
```

Abra seu navegador em [http://localhost:8000/docs](http://localhost:8000/docs) para acessar a interface interativa do Swagger.

---

## 📡 Endpoints da API

| Método | Rota | Descrição |
|---|---|---|
| `GET` | `/` | Identificação e status da API. |
| `POST` | `/v1/templates/inspect` | Inspeciona um `.xlsx` do Mercado Livre e retorna colunas, opções e prazo de validade. |
| `POST` | `/v1/workbooks/fill` | Recebe a planilha vazia + dados dos produtos em JSON e devolve o `.xlsx` preenchido para download. |
| `POST` | `/v1/photos/upload` | Faz upload de fotos para a API de imagens do Mercado Livre e retorna links HTTPS válidos. |
| `POST` | `/v1/listings/build` | **Uma chamada só:** planilha vazia + anúncios em JSON + imagens. Sobe as fotos, preenche a coluna `K` e devolve o `.xlsx`. |

---

## 🧾 Exemplo: camisetas em uma chamada

Os arquivos de [`examples/`](examples) usam o modelo de teste [`tests/fixtures/camiseta-09-06.xlsx`](tests/fixtures/camiseta-09-06.xlsx). Para publicar de verdade, troque pelo modelo que você acabou de baixar em **Anúncios > Anunciar em massa**.

Cada linha usa as **letras das colunas** da planilha. Numa camiseta, as obrigatórias são:

| Coluna | Campo | Exemplo |
|---|---|---|
| `A` | Título (até 60 caracteres) | `Camiseta Infantil Algodao Dinossauro Preta` |
| `C` | Condição | `Novo` |
| `D` | Cor | `Preto` |
| `G` | Tamanho | `2` |
| `H` | Marca | `Genérica` |
| `I` | Gênero | `Sem gênero infantil` |
| `J` | Código do guia de medidas | `1000001` |
| `K` | Fotos (URLs HTTPS separadas por vírgula) — ou `photos` no `/build` | |
| `L` | GTIN, ou um motivo da lista | `O produto não tem código cadastrado` |
| `N` | Estoque | `10` |
| `O` | Preço (8 a 1000000, até duas casas) | `39.90` |
| `S`, `U`, `V`, `W` | Tipo de anúncio, envio, custo de envio, retirada | `Clássico`, `Mercado Envios`, ... |
| `AA`–`AD` | Modelo, tipo de roupa, manga, material | `Dinossauro`, `Camiseta`, `Curta`, `Algodão` |

Use `POST /v1/templates/inspect` para ver todas as colunas e as opções aceitas do seu modelo.

### `/v1/listings/build` — modelo + anúncios + fotos

Cada linha cita suas imagens em `photos`, pelo nome do arquivo enviado em `images`. A API confere todas as linhas **antes** de subir qualquer foto, sobe cada imagem uma vez (mesmo usada em várias linhas) e preenche a `K` na ordem de `photos`.

```bash
curl -f -o anuncios.xlsx \
  -H "Authorization: Bearer $ML_ACCESS_TOKEN" \
  -F "template=@tests/fixtures/camiseta-09-06.xlsx" \
  -F "payload=<examples/build-camiseta.json" \
  -F "images=@frente.jpg;type=image/jpeg" \
  -F "images=@costas.jpg;type=image/jpeg" \
  http://localhost:8000/v1/listings/build
```

No PowerShell, use `curl.exe` e `$env:ML_ACCESS_TOKEN`, numa linha só.

O token só é exigido quando alguma linha tem `photos`. Uma linha que já traz a `K` pronta não sobe nada.

### `/v1/workbooks/fill` — modelo + anúncios com URLs prontas

```bash
curl -f -o anuncios.xlsx \
  -F "template=@tests/fixtures/camiseta-09-06.xlsx" \
  -F "payload=<examples/fill-camiseta.json" \
  http://localhost:8000/v1/workbooks/fill
```

### Erros

A API recusa a linha antes de gravar e lista **todos** os problemas:

```json
{
  "detail": "Listing row 1, column O must be between 8 and 1000000 with at most two decimals.",
  "code": "VALIDATION_ERROR",
  "errors": [
    {"row": 1, "column": "O", "message": "Listing row 1, column O must be between 8 and 1000000 with at most two decimals."},
    {"row": 2, "column": "K", "message": "Listing row 2, column K: photo 1 was not sent in images."}
  ]
}
```

Além das regras da própria planilha, o modelo de **Camisetas e Regatas** confere o que o Mercado Livre recusaria depois do envio: título até 60 caracteres, preço de 8 a 1000000, GTIN com dígito verificador, unidades do kit, prazo e unidade da garantia, descrição em texto simples e gênero/tamanho do guia de medidas.

| Status | Quando |
|---|---|
| `422` | linha inválida (lista completa em `errors`) |
| `401` | linha com `photos` sem token, ou token recusado pelo Mercado Livre |
| `413` | imagem acima de 10 MB ou mais de 200 imagens |
| `415` | imagem que não é PNG, JPEG ou WEBP |
| `400` | JSON inválido, imagem vazia ou duas imagens com o mesmo nome |
| `502` | um upload falhou no meio: nenhum `.xlsx` é devolvido e `uploaded` diz quantas fotos já subiram |

---

## 🧪 Rodando os Testes

```bash
pytest tests -v
```

---

## 🤝 Como Contribuir

Contribuições são muito bem-vindas! Se você deseja adicionar suporte a novas categorias, melhorar as validações ou sugerir melhorias:

1. Faça um Fork do projeto
2. Crie uma Branch para sua feature (`git checkout -b feature/nova-categoria`)
3. Faça o commit das alterações em inglês (`git commit -m 'feat: add footwear category profile'`)
4. Faça o push para a branch (`git push origin feature/nova-categoria`)
5. Abra um Pull Request

---

## 📄 Licença

Este projeto é distribuído sob a licença **MIT**. Consulte o arquivo [LICENSE](LICENSE) para mais detalhes.

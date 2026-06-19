# oxison

[![Release](https://img.shields.io/github/v/release/escotilha/oxison?label=release)](https://github.com/escotilha/oxison/releases/latest)
[![CI](https://github.com/escotilha/oxison/actions/workflows/ci.yml/badge.svg)](https://github.com/escotilha/oxison/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/github/license/escotilha/oxison)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

[🇬🇧 English](README.md) · **🇧🇷 Português (Brasil)**

**Aponte para um repositório — ou comece só de uma ideia — e ele escreve a documentação de produto (PRODUCT/MANUAL/STACK), planeja um roadmap e constrói o trabalho. Roda no Claude, Kimi ou Grok. Somente leitura por padrão, em sandbox quando escreve.**

> 🎉 **A v0.6.0 saiu** — memória entre execuções no build, `--integrate` seguro (compõe numa branch dedicada, nunca toca na sua `main`) e uma rodada completa de auditoria de segurança externa. Veja as [notas da release](https://github.com/escotilha/oxison/releases/tag/v0.6.0).

## Sobre

O `oxison` lê um repositório local, o compreende acionando a CLI do
[Claude Code](https://claude.com/claude-code) como um subprocesso
**somente leitura** e escreve artefatos de produto no seu próprio diretório de
saída. Ele **nunca modifica** o repositório que analisa.

```bash
oxison run /caminho/do/repo
# → ./oxison-output/{PRODUCT,MANUAL,STACK}.md
#   + ROADMAP-ANALYSIS.md (se o repo tiver um roadmap) ou SECURITY-NOTES.md (se não)
#   + COMPREHENSION.md + repomap.json + .oxison-run.json
```

### Modelo de segurança

- `oxison run` e `oxison plan` **nunca modificam o repositório-alvo.** O worker de
  IA é *estruturalmente* somente leitura (`--allowedTools Read,Glob,Grep` — sem
  shell, sem ferramentas de escrita: ele fisicamente não consegue alterar, criar,
  apagar ou executar nada) e o próprio oxison é dono de toda escrita,
  exclusivamente em `./oxison-output/`. Depois de um `run`/`plan`, a árvore de
  trabalho do git fica idêntica byte a byte (`git status` limpo, `HEAD` no lugar).
- `oxison build` é a exceção deliberada: ele **escreve código**. É contido por três
  camadas — um sandbox de sistema de arquivos + rede (ligado por padrão), limites
  de memória/processos por worker e gates de caminhos protegidos (lockfiles, configs
  de CI, `.git/`).
- Numa branch protegida (`main`/`master`), o `oxison build --integrate` compõe o
  roadmap numa branch dedicada `oxison/integration` e **nunca avança sua `main`** —
  você revisa e faz o `git merge` quando quiser.

## Requisitos

- **Python ≥ 3.11**
- A **CLI do [Claude Code](https://claude.com/claude-code)**, instalada e autenticada
  (o oxison a aciona como subprocesso; por padrão usa o seu login existente do
  Claude Code).

## Instalação

Não está no PyPI — instale direto do repositório:

```bash
# sem instalação, sempre na última versão (recomendado)
uvx --from git+https://github.com/escotilha/oxison oxison run /caminho/do/repo
# fixar numa release
pip install "git+https://github.com/escotilha/oxison.git@v0.6.0"
```

Extras de adaptadores de fonte (suporte a PDF, pptx e docx):

```bash
pip install "oxi-son[sources] @ git+https://github.com/escotilha/oxison.git@v0.6.0"
```

## Uso

Quatro subcomandos principais:

| Comando | O que faz |
|---|---|
| `oxison run <repo>` | Compreende o repo e escreve PRODUCT/MANUAL/STACK (+ análise de roadmap ou notas de segurança). Somente leitura. |
| `oxison ideate "<ideia>"` | Começa de uma ideia (greenfield), sem repo — gera os docs de produto e um roadmap. |
| `oxison plan` | Planeja um roadmap a partir de uma compreensão (ou direto do repo, somente leitura). |
| `oxison build <roadmap>` | Roda o loop de construção: workers escrevem código em worktrees isoladas e em sandbox. Use `--integrate` para compor numa branch dedicada. |

Exemplos:

```bash
# compreender um repositório existente
oxison run /caminho/do/repo

# começar de uma ideia
oxison ideate "um app de lista de tarefas com prazos e lembretes"

# ver o que seria construído, sem disparar nenhum worker
oxison build roadmap.json --dry-run
```

## Mais

Esta é uma introdução em português. A documentação completa — ingestão
multi-fonte (Oxicome), OCR, provedores de modelo (Kimi/Grok), autenticação,
planejamento (Oxipensa), construção (Oxfaz), custos, códigos de saída e como rodar
o oxison de dentro do próprio Claude Code — está no **[README em inglês](README.md)**.

Licença [MIT](LICENSE).

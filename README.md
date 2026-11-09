# Organizar Cenas — Insta360 Pro 2

Organiza os takes da Insta360 Pro 2 em pastas de cena.
Cada take tem 7 arquivos: 6 lentes (`origin_1_…mp4` a `origin_6_…mp4`) e o preview (`origin_…flv`).

## Baixar e instalar

1. Abra a página **Releases** deste repositório e baixe `Organizar-Cenas-mac.zip`.
2. Dê dois cliques no `.zip`. Arraste **Organizar Cenas.app** para a pasta **Aplicativos**.
3. **Só na primeira vez:** o Mac vai dizer que não pode verificar o desenvolvedor (o app não é assinado pela Apple).
   - **macOS 14 ou anterior:** clique com o botão direito no app → **Abrir** → **Abrir**.
   - **macOS 15 ou posterior:** tente abrir e clique em **OK**. Vá em **Ajustes do Sistema → Privacidade e Segurança**, role até o fim e clique em **Abrir Mesmo Assim**.

Funciona em Macs Apple Silicon (M1/M2/M3/M4) e Intel. Não precisa instalar Python.

## Como usar

1. **Origem:** a pasta com os cartões descarregados (ou os próprios cartões).
   **Pasta do projeto:** onde as cenas serão criadas. Precisa ser uma pasta separada, fora da origem.
2. Clique em **Varrer**. Aparece uma linha por take, com ✓ / ✗ para cada lente e para o preview.
   - Linha **vermelha** = problema: lente faltando, lente repetida ou sem preview.
3. Divida os takes em cenas:
   - **Sugerir cenas:** começa uma cena nova quando passam mais de X minutos entre um take e outro (ajustável). Antes de aplicar, mostra a proposta.
   - **Atribuir à cena…:** selecione takes (Shift+clique seleciona um intervalo). Depois escolha uma cena existente ou digite um nome novo, como "Cena 03 - Cozinha".
   - **Renomear cena…**, **Tirar da cena**, **Nota…** (ex.: "melhor take") e **Abrir preview** (precisa do VLC ou do IINA: o QuickTime não abre `.flv`).
4. **Simular** mostra o que vai acontecer sem alterar nada.
5. Escolha **Copiar** (padrão) ou **Mover** e clique em **Executar**.

Resultado:

```
Projeto/
  Cena 01 - Chegada/
    VID_20261108_162608/   ← 6 lentes + preview
    VID_20261108_163140/
  mapa_cenas.csv           ← take, data, hora, cena, nota, lentes presentes
  log_AAAAMMDD_HHMMSS.csv  ← o que foi feito com cada arquivo
```

## Segurança

- **Nunca sobrescreve.** Se um arquivo de mesmo nome já existe, salva como `nome (copia 1).mp4` e avisa antes.
- Cada arquivo tem o tamanho verificado depois de copiado. Para conferir também o SHA-256, marque **Verificar checksum** (mais lento).
- **Mover:** o original só é apagado depois que a cópia é verificada. Pede confirmação antes de começar.
- Confere se há espaço livre suficiente antes de começar.
- **Cancelar** para no meio do arquivo atual e descarta a cópia parcial.
- O mapa de cenas é salvo a cada mudança. Ao varrer de novo, as cenas voltam.
- Takes sem cena não são processados.

## Para quem mantém o código

- App: `organizar_cenas.py` (Python 3, só biblioteca padrão + tkinter). Rodar direto: `python3 organizar_cenas.py`.
- Testes: `python3 testes_cenas.py`.
- Build: `.github/workflows/build-mac.yml` gera o `.app` universal a cada push. Para publicar uma versão em Releases, crie uma tag: `git tag v1.0 && git push origin v1.0`.

#!/usr/bin/env bash
# Roda uma vez quando o Codespace é criado.
set -u

echo "==> Instalando Claude Code (instalador oficial)"
curl -fsSL https://claude.ai/install.sh | bash || echo "AVISO: falhou instalar o Claude Code"
grep -q '.local/bin' ~/.bashrc || echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc

echo "==> Baixando o dataset creditcard.csv (Kaggle)"
mkdir -p data
if [ -f data/creditcard.csv ]; then
  echo "dataset já existe"
elif [ -n "${KAGGLE_API_TOKEN:-}" ]; then
  pip install --quiet --user kaggle \
    && ~/.local/bin/kaggle datasets download -d mlg-ulb/creditcardfraud -p data --unzip \
    || echo "AVISO: falhou baixar o dataset"
else
  echo "AVISO: sem token do Kaggle; copie o creditcard.csv manualmente para data/"
fi

echo "==> Pronto. Rode 'claude' para começar."

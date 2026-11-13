#!/usr/bin/env bash
# Roda uma vez quando o Codespace é criado.
set -u

echo "==> Instalando Claude Code (instalador oficial)"
curl -fsSL https://claude.ai/install.sh | bash || echo "AVISO: falhou instalar o Claude Code"
grep -q '.local/bin' ~/.bashrc || echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc

echo "==> Instalando uv e as dependências do projeto"
pip install --quiet uv && uv sync --all-extras || echo "AVISO: falhou instalar dependências"

echo "==> Baixando o dataset creditcard.csv (Kaggle), usado só em 'make benchmark'"
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

echo "==> Pronto. 'make up' sobe o sistema; 'make demo' roda a demonstração."

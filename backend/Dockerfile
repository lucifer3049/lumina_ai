# syntax=docker/dockerfile:1
#
# backend image（02 §2、ADR-007：multi-stage、non-root、python-slim base）。
#
# 一個 image 多角色（12 §7）：api / worker / beat 共用本 image，差別只在 command。
# 因此這裡不寫死業務進程參數，預設 command 給 API，其餘由 compose / K8s 覆寫。
ARG PYTHON_IMAGE=python:3.12-slim-bookworm

# ── build stage：建立 .venv ──────────────────────────────────────
FROM ${PYTHON_IMAGE} AS builder

# uv 釘版：build 環境的工具版本漂移會讓「同一個 commit 產出不同 image」。
ARG UV_VERSION=0.11.32
ENV PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

RUN pip install --no-cache-dir "uv==${UV_VERSION}"

WORKDIR /app

# 先只複製鎖檔：原始碼改動不會使依賴層失效（build cache 命中率）。
COPY pyproject.toml uv.lock ./
# --frozen：lock 與 pyproject 不一致就失敗，不在 build 期偷偷解析新版本。
# --no-dev：image 不帶 pytest / ruff / mypy。
RUN uv sync --frozen --no-dev

COPY . .

# ── runtime stage ───────────────────────────────────────────────
FROM ${PYTHON_IMAGE} AS runtime

# non-root（ADR-007）。固定 uid 讓掛載卷的權限在不同環境一致。
RUN useradd --system --uid 10001 --create-home --shell /usr/sbin/nologin app

WORKDIR /app
COPY --from=builder --chown=app:app /app /app

# DJANGO_SETTINGS_MODULE 必須顯式指定：config/asgi.py 用的是
# `setdefault(..., "config.settings.dev")`，不設就等於**用 dev 設定跑部署**，
# 而那不會有任何症狀（dev 目前恰好等同 base）。理由詳見 config/settings/prod.py。
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DJANGO_SETTINGS_MODULE=config.settings.prod

USER app
EXPOSE 8000

# 設定與 secrets 一律由環境變數注入（鐵則 9），不進 image。
# 註：HEALTHCHECK 待 /healthz 端點存在後補（11 §4.2）。
#
# --log-config / --no-access-log 不是可選的調校，是日誌契約的一部分（12 §1.1）：
# 少了它們，uvicorn 會用自己的 handler（純文字、propagate=False）並另外輸出一份
# 欄位不同的 access log，結果是 Loki 只解析得了一半、每個請求兩筆記錄。
# 與 Makefile 的 dev / api / api-pinned 是同一組旗標，由
# tests/unit/test_logging.py 對帳所有啟動點——漏一處不會有錯誤訊息。
#
# worker 數刻意不寫死（見檔頭「一個 image 多角色」）：部署時以 --workers 覆寫，
# 01 附錄 A 註 1 的起始值是每 replica 2。
CMD ["uvicorn", "config.asgi:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--log-config", "config/uvicorn_logging.json", \
     "--no-access-log"]

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

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER app
EXPOSE 8000

# 設定與 secrets 一律由環境變數注入（鐵則 9），不進 image。
# 註：HEALTHCHECK 待 /healthz 端點存在後補（11 §4.2）。
CMD ["uvicorn", "config.asgi:app", "--host", "0.0.0.0", "--port", "8000"]

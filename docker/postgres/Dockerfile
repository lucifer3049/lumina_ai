# PostgreSQL 16 + pgvector + pgroonga（01 附錄 A：postgres 為自建 image）
#
# 為什麼自建：沒有官方 image 同時含這兩個 extension。
# - pgvector：向量檢索（05 §3.2，halfvec + HNSW）
# - pgroonga：中文全文檢索（05 §5.3，選它的理由是免自訂詞典的 CJK 斷詞）
#
# 基底選 pgvector 官方 image 而非「PGroonga image 再裝 pgvector」：
# pgvector image 是 PGDG 的 Debian bookworm 佈局，PGroonga 官方就有對應的
# `postgresql-16-pgdg-pgroonga` 套件；反過來則要自己編譯 pgvector。
#
# 版本一律釘死：image 漂移會讓「昨天還好好的」變成無法重現的環境問題
# （與 docker/compose.yml 釘 pgbouncer 版本同一個理由）。
ARG PGVECTOR_IMAGE=pgvector/pgvector:0.8.0-pg16
FROM ${PGVECTOR_IMAGE}

# PGroonga 的 apt 來源套件；`latest-bookworm` 由 PGroonga 官方維護，
# 內容是 apt repo 定義（非 pgroonga 本體），本體版本由下方 PGROONGA_VERSION 釘。
ARG GROONGA_APT_SOURCE_URL=https://packages.groonga.org/debian/groonga-apt-source-latest-bookworm.deb
# 釘版；升級時改這裡並重跑 tests/integration/test_infra_postgres.py。
# apt repo 只保留近期版本，若某天 build 報 "Version ... not found"，
# 改為空值（`--build-arg PGROONGA_VERSION=`）取最新版，量到版本號後回填。
ARG PGROONGA_VERSION=4.0.6-1
# FROM 之前宣告的 ARG 在 FROM 之後不可見，必須重新宣告才拿得到值。
ARG PGVECTOR_IMAGE

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates wget; \
    wget -q -O /tmp/groonga-apt-source.deb "${GROONGA_APT_SOURCE_URL}"; \
    apt-get install -y --no-install-recommends /tmp/groonga-apt-source.deb; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        "postgresql-16-pgdg-pgroonga${PGROONGA_VERSION:+=${PGROONGA_VERSION}}"; \
    # 建置結果留痕：出問題時 `docker run --rm IMAGE cat /etc/lumina-extensions` 即可比對版本
    { \
      echo "pgvector_image=${PGVECTOR_IMAGE}"; \
      dpkg-query -W -f 'pgroonga=${Version}\n' postgresql-16-pgdg-pgroonga; \
    } > /etc/lumina-extensions; \
    apt-get purge -y --auto-remove wget; \
    rm -rf /var/lib/apt/lists/* /tmp/groonga-apt-source.deb

# 註：`CREATE EXTENSION` 不在這裡做，也不用 docker-entrypoint-initdb.d。
# initdb.d 只在資料卷首次初始化時執行，pytest 另建的 test database 不會經過它，
# 於是本機綠、CI 紅。extension 一律由 Django migration 建立
# （apps/platform/migrations/0001_extensions.py，CLAUDE.md 鐵則 7）。

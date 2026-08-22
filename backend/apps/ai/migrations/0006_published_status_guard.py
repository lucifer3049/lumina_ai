"""不可變的 trigger 補上狀態這一半（2026-08-22 審查缺口；補 0003）。

`0003_published_is_immutable` 只在 ``OLD.status = 'published'`` 且內容三欄變動時擋。
狀態本身刻意放行（draft → published 是發佈，擋掉就沒有版本發得出去）——但那讓整個
保證可以**兩步繞過**：

    UPDATE ai_promptversion SET status = 'draft'  WHERE id = ...;   -- 內容沒動，放行
    UPDATE ai_promptversion SET template = '...'  WHERE id = ...;   -- OLD.status 已是 draft

而這個 trigger 存在的理由正是擋 Django Admin、``manage.py shell`` 與手動維運 SQL——那三
條路徑做兩次 UPDATE 毫無障礙。繞過之後，所有指向那個版本號的 `messages.prompt_version`
快照（05 §3.4）照樣變成謊，而資料庫裡看不出任何痕跡。

**補的是狀態的方向**：離開 draft 之後，``status`` 只准落在 ``published`` / ``archived``
之間。兩個方向的理由不同：

* 退回 ``draft`` 是上面那條繞過的第一步，沒有任何正當用途——要改內容就是新增版本。
* ``archived`` 必須放行（Phase 5 的發佈流程要淘汰舊版本），但**內容凍結一併延伸到
  archived**：只擋退回 draft 的話，``published → archived → 改內容`` 是同一個繞過，只是
  換了一個中繼站。archived 的版本正是歷史回答指過去最多的那一批。
* 未來若真的需要第三種狀態，這裡會當場失敗（而不是安靜放行），那時再一起想清楚。

`archived → published` 放行：內容既然凍結，重新啟用一個舊版本不會讓任何快照變成謊。
"""

from __future__ import annotations

from django.db import migrations

# `OLD.status <> 'draft'`：涵蓋 published 與 archived。0003 只看 published，而
# archived 的內容同樣被歷史回答指著。
FUNCTION = """
    CREATE OR REPLACE FUNCTION ai_promptversion_freeze_published()
    RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    BEGIN
        IF OLD.status <> 'draft' THEN
            IF NEW.template IS DISTINCT FROM OLD.template
                OR NEW.variables_schema IS DISTINCT FROM OLD.variables_schema
                OR NEW.model_hint IS DISTINCT FROM OLD.model_hint
            THEN
                RAISE EXCEPTION
                    '已發佈的 prompt 版本不可修改內容（prompt_version=%），請新增版本',
                    OLD.id
                    USING ERRCODE = 'restrict_violation';
            END IF;

            IF NEW.status NOT IN ('published', 'archived') THEN
                RAISE EXCEPTION
                    '已發佈的 prompt 版本不可改回 %（prompt_version=%）——退回草稿再改內容'
                    ' 等於繞過不可變，請新增版本',
                    NEW.status, OLD.id
                    USING ERRCODE = 'restrict_violation';
            END IF;
        END IF;

        RETURN NEW;
    END;
    $$;
"""

# 0003 的函式原文（rollback 要回得去原狀，含它的兩步繞過）。
REVERSE = """
    CREATE OR REPLACE FUNCTION ai_promptversion_freeze_published()
    RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    BEGIN
        IF OLD.status = 'published' AND (
               NEW.template IS DISTINCT FROM OLD.template
            OR NEW.variables_schema IS DISTINCT FROM OLD.variables_schema
            OR NEW.model_hint IS DISTINCT FROM OLD.model_hint
        ) THEN
            RAISE EXCEPTION
                '已發佈的 prompt 版本不可修改內容（prompt_version=%），請新增版本',
                OLD.id
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN NEW;
    END;
    $$;
"""


class Migration(migrations.Migration):
    dependencies = [("ai", "0005_rls_write_scope")]

    # trigger 本身不重建：0003 建的 `BEFORE UPDATE` trigger 指向同名函式，
    # `CREATE OR REPLACE FUNCTION` 就地換掉函式體即可。
    operations = [migrations.RunSQL(sql=FUNCTION, reverse_sql=REVERSE)]

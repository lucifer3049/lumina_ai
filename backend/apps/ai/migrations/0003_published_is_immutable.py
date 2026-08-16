"""published 版本不可變（05 §3.3：「published 後不可變（DB trigger 拒絕 UPDATE template）」）。

**為什麼是 trigger 而不是 Service 的判斷。** `messages.prompt_version`（05 §3.4）是每
一則回答的快照，而「這個回答當時用了什麼指令」必須永遠指得回同一份內容——那是 06 §1
的「版本化貫穿」要保證的事，也是事故回溯與 3B 評測的前提。

Service 那一層擋得住我們自己寫的程式碼，擋不住三條真實存在的路徑：Django Admin
（2C 明訂用它頂替管理面）、`manage.py shell`、以及手動的維運 SQL。而那三條正是
「線上答得怪怪的，我先臨時改一下 prompt」最常發生的地方——改完之後，所有指向那個
版本號的歷史回答都變成了謊。

**被凍結的是內容三欄**（template / variables_schema / model_hint）。狀態本身可以改：
draft → published 是**發佈**，把它一起擋掉的話，沒有任何版本發佈得出去，而那要到第
一次發佈時才會發現。`published_at` / `published_by` / `change_note` 同理，它們記的是
發佈這個動作。

要修正一份已發佈的模板，就是新增一個版本並切換 active——那才會在 `messages` 留下
可分辨的痕跡。
"""

from __future__ import annotations

from django.db import migrations

FUNCTION = """
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

    DROP TRIGGER IF EXISTS ai_promptversion_freeze_published ON ai_promptversion;
    CREATE TRIGGER ai_promptversion_freeze_published
        BEFORE UPDATE ON ai_promptversion
        FOR EACH ROW
        EXECUTE FUNCTION ai_promptversion_freeze_published();
"""

REVERSE = """
    DROP TRIGGER IF EXISTS ai_promptversion_freeze_published ON ai_promptversion;
    DROP FUNCTION IF EXISTS ai_promptversion_freeze_published();
"""


class Migration(migrations.Migration):
    dependencies = [("ai", "0002_rls")]

    operations = [migrations.RunSQL(sql=FUNCTION, reverse_sql=REVERSE)]

"""白名單內的內容型別常數 —— 上傳判定與 loader 選擇共用的單一事實來源。

放在 `core/` 而不是任一端，是因為這三個字串同時是**兩件事的鍵**：上傳時的白名單
（`services/knowledge/uploads.py`）、以及抽取時挑哪個 loader（`etl/extract/`）。
兩邊各寫一份的話，新增格式時漏改一邊的症狀是「上傳收得下、ETL 說不支援」——文件
會停在 failed，而錯誤訊息指向的是 loader，不是那個真正漏掉的地方。

`etl/` 不得 import `services/`（鐵則 2），因此共用點只能在更內圈；`core/` 是兩者
都看得到的最內圈。
"""

from __future__ import annotations

PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
TEXT = "text/plain"

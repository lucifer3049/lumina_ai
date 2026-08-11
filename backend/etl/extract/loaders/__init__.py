"""來源 → `ExtractedDoc` 的 loader（08 §3）。

**loader 的職責只有一件事：把來源的結構翻譯成 block。** 清洗（頁首頁尾偵測、亂碼
丟棄）與切塊都在下游（1B-5）——混進 loader 的話，每加一種來源就要重寫一次同樣的
清洗規則，而 08 §1 的「下游完全來源無關」也就不成立了。

每個 loader 的簽章相同（``bytes -> LoadResult``），註冊表在 `etl.extract`。
"""

from __future__ import annotations

from etl.extract.loaders.base import Loader, LoadResult

__all__ = ["LoadResult", "Loader"]

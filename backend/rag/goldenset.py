"""Golden set 與語料檔的解析（13 §4 工作包 2B-0；Phase 2 DoD ②）。

**題組是評測的量尺，而壞掉的量尺不會發出任何聲音。** 一題指到不存在的段落，它的 recall
永遠是 0；十題這樣的話整份報告會低 10 分，而看起來只是「檢索沒那麼準」。因此這一層的
態度與 `services/rag/params.py` **相反**：那裡是熱路徑上的使用者設定，壞值退回預設並記
一筆；這裡是離線評測的輸入，**壞了就當場炸**——沒有人在等這個回應，而一個安靜的預設值
會變成一份沒有人懷疑的分數。

格式是 JSONL（一行一筆）：語料與題組都會長到數千行，diff 時看得出「改了哪幾筆」比
「整份 JSON 重排」有用得多。

- 語料：`{"passage_id": str, "title": str, "text": str}`
- 題組：`{"question_id": str, "question": str, "passage_ids": [str, ...],
   "language": str, "source": str}`

`sha256` 是**整個檔案的位元組雜湊**，用途只有一個：判定兩份評測報告可不可比
（`scripts/eval_retrieval.py` 的 `compare_reports`）。題組或語料動過之後，舊 baseline
的分數就不再是同一把尺量出來的，而兩個數字照樣相減得出來。

`language` / `source` 的**允許值不寫在這裡**：那份清單屬於題組的內容規範（由
`tests/unit/test_golden_set.py` 守著）。寫進解析器的話，新增一種語言要改程式，而漏改的
症狀是「載入失敗」——那是最不需要程式碼參與的一種決定。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "Corpus",
    "GoldenSet",
    "GoldenSetError",
    "Passage",
    "Question",
    "load_corpus",
    "load_goldenset",
    "validate",
]


class GoldenSetError(Exception):
    """題組或語料檔壞掉。

    **不繼承 `core.exceptions` 的業務例外階層**：那一套的用途是把錯誤翻成 API 的回應碼，
    而這裡的讀者只有離線腳本與測試。掛上去只會讓「評測資料檔打錯字」有機會變成某個
    端點的 4xx。
    """


@dataclass(frozen=True, slots=True)
class Passage:
    """語料的一段——**評測的計分單位**。

    2B-0 的設計決定：語料一段 = 知識庫的一個 chunk（不走 chunker）。切塊器會把長段落切
    開，於是「正解段落」在 DB 裡變成三個 chunk，recall 的分母是段落數而命中是 chunk 數
    ——兩邊對不齊，分數不再有意義。
    """

    passage_id: str
    title: str
    text: str


@dataclass(frozen=True, slots=True)
class Corpus:
    name: str
    passages: tuple[Passage, ...]
    sha256: str

    def __len__(self) -> int:
        return len(self.passages)

    def get(self, passage_id: str) -> Passage | None:
        return self._by_id().get(passage_id)

    def ids(self) -> frozenset[str]:
        return frozenset(self._by_id())

    def _by_id(self) -> Mapping[str, Passage]:
        # 每次重建而不是快取：語料最多數千筆，而 frozen dataclass 要放一份快取就得動用
        # `object.__setattr__`——為了這個規模的省事把不可變性打一個洞不划算。
        return {passage.passage_id: passage for passage in self.passages}


@dataclass(frozen=True, slots=True)
class Question:
    """一題：問句 + 哪些段落算對。

    `passage_ids` 是**集合**：名次由檢索決定，正解之間沒有順序。寫成 list 的話，「兩個
    正解的順序」會變成一個看起來有意義、實際上沒有人維護的東西。
    """

    question_id: str
    question: str
    passage_ids: frozenset[str]
    language: str
    source: str


@dataclass(frozen=True, slots=True)
class GoldenSet:
    name: str
    questions: tuple[Question, ...]
    sha256: str

    def __len__(self) -> int:
        return len(self.questions)


def load_corpus(path: Path) -> Corpus:
    passages: list[Passage] = []
    seen: set[str] = set()
    for line_no, row in _rows(path):
        passage_id = _text_field(row, "passage_id", path, line_no)
        if passage_id in seen:
            # 重複的 id 會讓後者覆蓋前者（或讓命中對回錯的段落），而筆數對得上。
            raise GoldenSetError(f"{path.name}:{line_no} passage_id 重複：{passage_id}")
        seen.add(passage_id)
        passages.append(
            Passage(
                passage_id=passage_id,
                # title 允許是空字串（有些來源沒有標題），但欄位必須在——少一個欄位
                # 通常代表這份檔案是用另一種格式產生的。
                title=_text_field(row, "title", path, line_no, allow_blank=True),
                text=_text_field(row, "text", path, line_no),
            )
        )

    if not passages:
        raise GoldenSetError(f"{path.name} 沒有任何段落")
    return Corpus(name=path.stem, passages=tuple(passages), sha256=_fingerprint(path))


def load_goldenset(path: Path) -> GoldenSet:
    questions: list[Question] = []
    seen: set[str] = set()
    for line_no, row in _rows(path):
        question_id = _text_field(row, "question_id", path, line_no)
        if question_id in seen:
            # 題組會被合併成一份報告，id 撞在一起時題數對得上而內容少一題。
            raise GoldenSetError(f"{path.name}:{line_no} question_id 重複：{question_id}")
        seen.add(question_id)
        questions.append(
            Question(
                question_id=question_id,
                question=_text_field(row, "question", path, line_no),
                passage_ids=_passage_ids(row, path, line_no, question_id),
                language=_text_field(row, "language", path, line_no),
                source=_text_field(row, "source", path, line_no),
            )
        )

    if not questions:
        raise GoldenSetError(f"{path.name} 沒有任何題目")
    return GoldenSet(name=path.stem, questions=tuple(questions), sha256=_fingerprint(path))


def validate(goldenset: GoldenSet, corpus: Corpus) -> None:
    """題組的每一個正解都必須在語料裡（缺一即 raise）。

    **一次列出所有缺的 id**，不是遇到第一個就停：這類錯誤通常來自取樣腳本的一個 bug，
    一次修完比修一次跑一次快得多。
    """
    known = corpus.ids()
    missing = {
        question.question_id: sorted(question.passage_ids - known)
        for question in goldenset.questions
        if not question.passage_ids <= known
    }
    if missing:
        detail = "；".join(f"{qid} → {', '.join(ids)}" for qid, ids in sorted(missing.items()))
        raise GoldenSetError(
            f"{goldenset.name} 有 {len(missing)} 題指向 {corpus.name} 裡不存在的段落：{detail}"
        )


def _rows(path: Path) -> Iterator[tuple[int, Mapping[str, Any]]]:
    if not path.exists():
        raise GoldenSetError(f"找不到資料檔：{path}")

    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as exc:
            # 帶行號：數千行的檔案裡，「哪一行壞了」是唯一有用的資訊。
            raise GoldenSetError(f"{path.name}:{line_no} 不是合法的 JSON：{exc}") from exc
        if not isinstance(parsed, dict):
            raise GoldenSetError(f"{path.name}:{line_no} 應該是一個物件，收到 {type(parsed)}")
        yield line_no, parsed


def _text_field(
    row: Mapping[str, Any], key: str, path: Path, line_no: int, *, allow_blank: bool = False
) -> str:
    value = row.get(key)
    if not isinstance(value, str):
        raise GoldenSetError(f"{path.name}:{line_no} 缺少字串欄位 {key}")
    if not allow_blank and not value.strip():
        raise GoldenSetError(f"{path.name}:{line_no} 的 {key} 是空的")
    return value


def _passage_ids(
    row: Mapping[str, Any], path: Path, line_no: int, question_id: str
) -> frozenset[str]:
    value = row.get("passage_ids")
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise GoldenSetError(f"{path.name}:{line_no} 的 passage_ids 必須是字串陣列")
    if not value:
        # 沒有正解的題目算不出 recall（分母 0）。它會被算成 0 分或被靜默跳過，而那
        # 看起來像「檢索找不到」——真相是題組壞了。`rag/metrics.py` 是第二道。
        raise GoldenSetError(f"{path.name}:{line_no} 題目 {question_id} 沒有指定任何正解段落")
    return frozenset(str(item) for item in value)


def _fingerprint(path: Path) -> str:
    """整個檔案的 sha256（見模組 docstring）。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()

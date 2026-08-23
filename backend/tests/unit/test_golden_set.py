"""驗收：golden set 與語料檔本身（13 §4 工作包 2B-0；Phase 2 DoD ②「golden set ≥100 題」）。

**題組是評測的量尺，而壞掉的量尺不會發出任何聲音。** 一題指到不存在的段落，它的 recall
永遠是 0；十題這樣的話，整份報告會低 10 分，而看起來只是「檢索沒那麼準」。2B 之後每一次
「hybrid 好了多少」的判斷都要拿這份題組去量，所以它自己得先有人守。

**語料是凍結的快照，不是即時讀 `docs/plan/`**（2B-0 開工前的決定）。評測語料一旦會隨
文件更新而變動，兩次評測的分數就不可比——而不可比的兩個數字看起來仍然可以相減。凍結的
代價是它會過時；那是刻意的，過時的量尺至少量得出變化。

**公開題組為主的理由**（2026-08-23 拍板）：問題出自人手（DRCD 這類人寫問句 + 標好答案
段落的資料集），不是用 LLM 從 chunk 生成。生成的問句會沿用段落原文的字詞，那等於天然
偏袒字面檢索（FTS）——而「hybrid 是否優於純向量」正是這份題組要回答的問題，先偏袒一邊
的話，2B-2 的結論不管是什麼都不能信。自家文件的手寫題補的是公開集給不了的兩件事：真實
文體，以及跨語言（英文問句、中文段落——06 §3.4 指名 rerank 必須多語的那個情境）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rag.goldenset import GoldenSetError, load_corpus, load_goldenset, validate

BACKEND_ROOT = Path(__file__).resolve().parents[2]
EVALUATION_ROOT = BACKEND_ROOT / "evaluation"

PUBLIC_CORPUS = EVALUATION_ROOT / "corpus" / "drcd.jsonl"
PUBLIC_QUESTIONS = EVALUATION_ROOT / "goldenset" / "drcd.jsonl"
OWN_CORPUS = EVALUATION_ROOT / "corpus" / "lumina_docs.jsonl"
OWN_QUESTIONS = EVALUATION_ROOT / "goldenset" / "handwritten.jsonl"
README = EVALUATION_ROOT / "README.md"

_PAIRS = ((PUBLIC_CORPUS, PUBLIC_QUESTIONS), (OWN_CORPUS, OWN_QUESTIONS))

# 允許值寫死在測試裡而不是從資料檔推導：推導的話，打錯字的 `zh-hant` 會自己變成一個
# 合法的新語言，而跨語言那幾題的斷言就跟著失效。
_LANGUAGES = {"zh-Hant", "en"}
_SOURCES = {"drcd", "handwritten"}


class TestFilesAreThere:
    def test_the_four_data_files_and_the_readme_exist(self) -> None:
        for path in (PUBLIC_CORPUS, PUBLIC_QUESTIONS, OWN_CORPUS, OWN_QUESTIONS, README):
            assert path.exists(), f"缺少 {path.relative_to(BACKEND_ROOT)}"

    def test_the_readme_records_where_the_public_data_came_from(self) -> None:
        """出處、授權與凍結日期缺一不可。

        公開資料集帶授權條件（DRCD 是 CC BY-SA 3.0，要求標示出處），而這份語料會一直
        留在 repo 裡。凍結日期則是「這份快照對應哪個版本的文件」的唯一線索——沒有它，
        半年後沒有人說得出手寫那 20 題當初是照哪一版出的。
        """
        text = README.read_text(encoding="utf-8")

        assert "http" in text, "README 沒有指向公開資料集的出處連結"
        assert "CC BY" in text, "README 沒有寫明公開資料集的授權"
        assert "2026-" in text, "README 沒有寫明語料的凍結日期"


class TestSize:
    def test_the_public_set_has_at_least_one_hundred_questions(self) -> None:
        """Phase 2 DoD ② 的數字（13 §4）。"""
        assert len(load_goldenset(PUBLIC_QUESTIONS).questions) >= 100

    def test_the_public_corpus_is_big_enough_to_be_discriminating(self) -> None:
        """語料至少 1,000 段。

        **題數達標不代表這把尺量得出東西**：語料只有 100 段（剛好是 100 題的正解）時，
        top_k=10 等於一次撈走十分之一的語料，recall 會逼近 1.0——純向量、hybrid、加不加
        rerank 全部滿分，而 DoD ②「hybrid 優於純向量」就永遠證不出來，也永遠推翻不了。
        干擾段落不是雜訊，是這份題組的解析度。
        """
        assert len(load_corpus(PUBLIC_CORPUS).passages) >= 1000

    def test_the_handwritten_set_has_at_least_twenty_questions(self) -> None:
        assert len(load_goldenset(OWN_QUESTIONS).questions) >= 20

    def test_the_handwritten_set_covers_cross_language_retrieval(self) -> None:
        """英文問句配中文段落——至少三題。

        跨語言是 06 §3.4 對 rerank 的**硬性條件**（單語 reranker 會把跨語言的正確候選
        打低分，比不 rerank 更糟），也是 06 §3.1 說 FTS 在跨語言會失效、要靠向量撐住的
        那個情境。題組裡沒有這類題目的話，2B-1 接上 FTS 之後品質退化在數字上看不出來。
        """
        questions = load_goldenset(OWN_QUESTIONS).questions
        english = [question for question in questions if question.language == "en"]

        assert len(english) >= 3, "手寫題組缺跨語言題（英文問句 → 中文段落）"


class TestContent:
    @pytest.mark.parametrize(("corpus_path", "questions_path"), _PAIRS)
    def test_every_question_points_at_passages_that_exist(
        self, corpus_path: Path, questions_path: Path
    ) -> None:
        """指到不存在的段落 = 那題永遠 0 分，而報告上看起來像檢索找不到。"""
        validate(load_goldenset(questions_path), load_corpus(corpus_path))

    def test_question_ids_are_unique_across_both_files(self) -> None:
        """兩份題組會被合併成一份報告，id 撞在一起時後者會覆蓋前者——題數對得上，
        內容少一題。"""
        ids = [
            question.question_id
            for path in (PUBLIC_QUESTIONS, OWN_QUESTIONS)
            for question in load_goldenset(path).questions
        ]

        assert len(ids) == len(set(ids))

    @pytest.mark.parametrize("path", (PUBLIC_CORPUS, OWN_CORPUS))
    def test_passages_are_unique_in_id_and_in_text(self, path: Path) -> None:
        """**內容重複的段落是評測的毒**：正解是 A，檢索回了內容一模一樣的 B，會被算成
        沒命中。取樣時就該去掉，而不是事後解釋分數。"""
        passages = load_corpus(path).passages
        ids = [passage.passage_id for passage in passages]
        texts = [passage.text for passage in passages]

        assert len(ids) == len(set(ids))
        assert len(texts) == len(set(texts))

    @pytest.mark.parametrize("path", (PUBLIC_QUESTIONS, OWN_QUESTIONS))
    def test_language_and_source_come_from_a_fixed_vocabulary(self, path: Path) -> None:
        for question in load_goldenset(path).questions:
            assert question.language in _LANGUAGES, f"{question.question_id} 的語言標記不合法"
            assert question.source in _SOURCES, f"{question.question_id} 的來源標記不合法"

    @pytest.mark.parametrize("path", (PUBLIC_CORPUS, OWN_CORPUS))
    def test_passage_text_is_not_blank(self, path: Path) -> None:
        for passage in load_corpus(path).passages:
            assert passage.text.strip(), f"{passage.passage_id} 是空段落"


class TestLoader:
    """壞資料要在載入時就炸，而不是變成一個難看的分數。"""

    def _write(self, path: Path, rows: list[dict[str, object]]) -> Path:
        path.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
            encoding="utf-8",
        )
        return path

    def test_a_question_without_relevant_passages_is_rejected(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path / "bad.jsonl",
            [
                {
                    "question_id": "q1",
                    "question": "問句",
                    "passage_ids": [],
                    "language": "zh-Hant",
                    "source": "handwritten",
                }
            ],
        )

        with pytest.raises(GoldenSetError):
            load_goldenset(path)

    def test_duplicate_question_ids_are_rejected(self, tmp_path: Path) -> None:
        row: dict[str, object] = {
            "question_id": "q1",
            "question": "問句",
            "passage_ids": ["p1"],
            "language": "zh-Hant",
            "source": "handwritten",
        }
        path = self._write(tmp_path / "dup.jsonl", [row, dict(row)])

        with pytest.raises(GoldenSetError):
            load_goldenset(path)

    def test_a_missing_field_is_rejected(self, tmp_path: Path) -> None:
        path = self._write(tmp_path / "partial.jsonl", [{"question_id": "q1", "question": "問句"}])

        with pytest.raises(GoldenSetError):
            load_goldenset(path)

    def test_validate_rejects_a_question_pointing_at_a_missing_passage(
        self, tmp_path: Path
    ) -> None:
        corpus = self._write(
            tmp_path / "corpus.jsonl",
            [{"passage_id": "p1", "title": "標題", "text": "內容"}],
        )
        questions = self._write(
            tmp_path / "questions.jsonl",
            [
                {
                    "question_id": "q1",
                    "question": "問句",
                    "passage_ids": ["p404"],
                    "language": "zh-Hant",
                    "source": "handwritten",
                }
            ],
        )

        with pytest.raises(GoldenSetError, match="p404"):
            validate(load_goldenset(questions), load_corpus(corpus))


class TestFingerprint:
    """`sha256` 決定兩份評測報告可不可比（見 `test_eval_runner.py`）。"""

    def test_the_same_bytes_give_the_same_fingerprint(self, tmp_path: Path) -> None:
        rows = [{"passage_id": "p1", "title": "標題", "text": "內容"}]
        first = tmp_path / "a.jsonl"
        second = tmp_path / "b.jsonl"
        for path in (first, second):
            path.write_text(json.dumps(rows[0], ensure_ascii=False) + "\n", encoding="utf-8")

        assert load_corpus(first).sha256 == load_corpus(second).sha256

    def test_changing_one_character_changes_the_fingerprint(self, tmp_path: Path) -> None:
        """題組改過之後就不該再跟舊 baseline 相比——這個 hash 是唯一擋得住的東西。"""
        path = tmp_path / "c.jsonl"
        path.write_text('{"passage_id": "p1", "title": "t", "text": "A"}\n', encoding="utf-8")
        before = load_corpus(path).sha256

        path.write_text('{"passage_id": "p1", "title": "t", "text": "B"}\n', encoding="utf-8")

        assert load_corpus(path).sha256 != before

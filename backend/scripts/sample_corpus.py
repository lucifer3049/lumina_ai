"""產生評測用的語料與題組（13 §4 工作包 2B-0）。

**這支腳本的產物是要提交進 repo 的凍結快照，不是每次跑評測都重跑的東西。** 評測語料
一旦會隨資料集版本或文件更新而變動，兩次評測的分數就不可比——而不可比的兩個數字看起來
仍然可以相減。因此：

- **取樣是決定性的**（固定亂數種子）。同樣的輸入永遠得到位元組相同的輸出，`sha256`
  才有意義（`compare_reports` 用它判定兩份報告可不可比）。
- **重跑會蓋掉檔案，也就會讓所有既有的 baseline 失效**。真的要重取樣時，記得同時重跑
  baseline（`make eval-retrieval`），否則 `tests/unit/test_eval_runner.py` 會紅——那正是
  它存在的目的。

兩個來源：

    python scripts/sample_corpus.py drcd    # 公開題組：人寫的問句 + 標好的答案段落
    python scripts/sample_corpus.py docs    # 自家文件的段落快照（手寫題的語料）

`drcd` 產出語料與題組兩個檔；`docs` 只產出語料——那 20 題由人手寫（出題的人不該是
被評測的系統，也不該是替它寫程式的那一個）。

**不需要 Django**：這裡只讀寫檔案。`scripts/eval_retrieval.py` 才碰 DB。
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
import urllib.request
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from rag.goldenset import load_corpus, load_goldenset, validate  # noqa: E402

REPO_ROOT = BACKEND_ROOT.parent
EVALUATION_ROOT = BACKEND_ROOT / "evaluation"
# 產物的路徑**登記在 `scripts/eval_retrieval.py` 的 `DATASETS`**；這裡是第二份。
# 兩份漂掉的症狀很明顯（評測說「找不到資料檔」、`test_golden_set.py` 整片紅），
# 所以不值得為它多做一層抽象。
CORPUS_DIR = EVALUATION_ROOT / "corpus"
GOLDENSET_DIR = EVALUATION_ROOT / "goldenset"
CACHE_DIR = EVALUATION_ROOT / ".cache"

# DRCD（Delta Reading Comprehension Dataset）：繁體中文閱讀理解，CC BY-SA 3.0。
#
# **dev + test 兩個 split**，不用 training：兩者合計 4 MB、約 2,000 段，取 1,200 段還有
# 餘裕；training 是 15 MB，而多下載 11 MB 換不到任何評測價值。只用 test 則不夠——它過濾
# 後剩 1,000 段，剛好卡在 `test_golden_set.py` 的 ≥1,000 下限上，補一題就會失敗。
_DRCD_BASE = "https://raw.githubusercontent.com/DRCKnowledgeTeam/DRCD/master"
DRCD_FILES = ("DRCD_dev.json", "DRCD_test.json")

# 種子寫死在程式裡而不是參數的預設值：它是產物的一部分（換了種子就是另一份語料），
# 而參數預設值會讓人以為它可以隨手調。
_SEED = 20260823

# 段落長度的上下限（字元）。太短的段落沒有足以回答問題的資訊，太長的會在切塊那一層
# 被當成需要拆的東西——而評測的前提是「一段 = 一個 chunk」（見 eval_retrieval 的
# 模組 docstring）。
_MIN_CHARS = 120
_MAX_CHARS = 1500


@dataclass(frozen=True, slots=True)
class _Paragraph:
    passage_id: str
    title: str
    text: str
    questions: tuple[tuple[str, str], ...]  # (question_id, question)


# ── DRCD ───────────────────────────────────────────────────────


def sample_drcd(
    *,
    sources: Sequence[Path],
    questions: int,
    passages: int,
    corpus_out: Path,
    goldenset_out: Path,
) -> None:
    """DRCD → 語料 + 題組。

    **一段最多出一題**：同一段被兩題指到的話，那一段的檢索難度會被算兩次，而題組看起來
    有 120 題、實際上只覆蓋了 100 段。覆蓋面才是評測的解析度。

    **干擾段落不是雜訊，是解析度**：語料只有正解那 120 段時，`top_k=10` 等於一次撈走
    十二分之一，所有模式都會接近滿分，而 DoD ②「hybrid 優於純向量」就永遠證不出來。
    """
    parsed = _read_drcd(sources)
    if len(parsed) < passages:
        raise SystemExit(f"可用段落只有 {len(parsed)} 段，不足 {passages} 段")

    # S311：這裡要的正是「可重現的偽亂數」——換成 `secrets` 會讓每次取樣得到不同的
    # 語料，而那正是本檔開頭那段警告要避免的事。
    rng = random.Random(_SEED)  # noqa: S311
    shuffled = list(parsed)
    rng.shuffle(shuffled)

    gold: list[_Paragraph] = []
    rows: list[dict[str, Any]] = []
    seen_questions: set[str] = set()
    for paragraph in shuffled:
        if len(gold) >= questions:
            break
        question = next(
            (
                (qid, text)
                for qid, text in paragraph.questions
                # 極短的問句多半是資料本身的殘缺（「是什麼？」），它們量到的是運氣。
                if len(text.strip()) >= 8 and text.strip() not in seen_questions
            ),
            None,
        )
        if question is None:
            continue
        qid, text = question
        seen_questions.add(text.strip())
        gold.append(paragraph)
        rows.append(
            {
                "question_id": f"drcd-{qid}",
                "question": text.strip(),
                "passage_ids": [paragraph.passage_id],
                "language": "zh-Hant",
                "source": "drcd",
            }
        )

    if len(gold) < questions:
        raise SystemExit(f"只湊到 {len(gold)} 題，不足 {questions} 題")

    chosen = {paragraph.passage_id for paragraph in gold}
    distractors = [p for p in shuffled if p.passage_id not in chosen][: passages - len(gold)]
    corpus = sorted([*gold, *distractors], key=lambda p: p.passage_id)

    _write(
        corpus_out,
        [{"passage_id": p.passage_id, "title": p.title, "text": p.text} for p in corpus],
    )
    _write(goldenset_out, sorted(rows, key=lambda row: str(row["question_id"])))
    _verify(corpus_out, goldenset_out)


def _read_drcd(paths: Sequence[Path]) -> list[_Paragraph]:
    seen_ids: set[str] = set()
    seen_texts: set[str] = set()
    paragraphs: list[_Paragraph] = []

    # 跨檔去重：dev 與 test 是不同的 split，但兩邊都可能收錄同一篇文章的段落。
    for article in (a for path in paths for a in _articles(path)):
        title = str(article.get("title", "")).strip()
        for paragraph in article.get("paragraphs", []):
            text = str(paragraph.get("context", "")).strip()
            passage_id = f"drcd-{paragraph.get('id')}"
            # **內容重複的段落是評測的毒**：正解是 A，檢索回了內容一模一樣的 B，會被
            # 算成沒命中。在取樣這一步剔掉，而不是事後解釋分數。
            if not _MIN_CHARS <= len(text) <= _MAX_CHARS:
                continue
            if passage_id in seen_ids or text in seen_texts:
                continue
            seen_ids.add(passage_id)
            seen_texts.add(text)
            paragraphs.append(
                _Paragraph(
                    passage_id=passage_id,
                    title=title,
                    text=text,
                    questions=tuple(
                        (str(qa.get("id")), str(qa.get("question", "")))
                        for qa in paragraph.get("qas", [])
                    ),
                )
            )
    # 依 id 排序：JSON 的順序目前是穩定的，但它不是我們能保證的東西，而取樣的決定性
    # 建立在「輸入順序固定」之上。
    return sorted(paragraphs, key=lambda p: p.passage_id)


def _articles(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return list(payload.get("data", []))


def _download(url: str, target: Path) -> Path:
    """下載原始資料集到快取（已存在就不重抓）。

    快取在 `evaluation/.cache/`（gitignore）：原始檔 2 MB、我們提交的是取樣後的子集，
    整份進版控只是讓 clone 變慢。
    """
    if target.exists():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    print(f"下載 {url}")
    with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310
        target.write_bytes(response.read())
    return target


# ── 自家文件 ────────────────────────────────────────────────────


def sample_docs(*, source_dir: Path, per_file: int, limit: int, corpus_out: Path) -> None:
    """`docs/plan/*.md` → 段落快照（手寫題的語料）。

    **快照而不是即時讀**：文件會改，而題組的正解錨在 passage_id 上。即時讀的話，改一次
    文件就會有幾題悄悄變成「正解不存在」——recall 掉下去，看起來像檢索退步。

    取樣在每個檔案內**等距抽**而不是取前 N 段：取前面的話，抽到的全是每份文件的前言與
    目錄，而那幾段長得都一樣（「本文件說明…」），檢索起來難以區辨。
    """
    kept: list[dict[str, str]] = []
    for path in sorted(source_dir.glob("*.md")):
        blocks = list(_markdown_blocks(path))
        if not blocks:
            continue
        stride = max(1, math.ceil(len(blocks) / per_file))
        for index in range(0, len(blocks), stride):
            title, text = blocks[index]
            kept.append(
                {
                    "passage_id": f"{path.stem}#{index}",
                    "title": f"{path.name} § {title}" if title else path.name,
                    "text": text,
                }
            )

    if len(kept) > limit:
        kept = kept[:limit]
    _write(corpus_out, sorted(kept, key=lambda row: row["passage_id"]))
    corpus = load_corpus(corpus_out)
    print(f"語料 {_display(corpus_out)}：{len(corpus.passages)} 段 sha256={corpus.sha256[:12]}…")


def _markdown_blocks(path: Path) -> Iterator[tuple[str, str]]:
    """Markdown → (標題路徑, 區塊文字)。

    區塊的邊界是空行；連續的表格列算一個區塊（拆開的話，每一列都是沒有主詞的碎片）；
    ```圍起來的程式碼與 mermaid 圖跳過——它們是圖，不是可以拿來問答的文字。
    """
    heading: list[str] = []
    buffer: list[str] = []
    in_fence = False

    def flush() -> Iterator[tuple[str, str]]:
        text = "\n".join(buffer).strip()
        buffer.clear()
        if _MIN_CHARS <= len(text) <= _MAX_CHARS:
            yield " / ".join(heading), text

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        if line.startswith("```"):
            in_fence = not in_fence
            buffer.clear()
            continue
        if in_fence:
            continue
        if line.startswith("#"):
            yield from flush()
            level = len(line) - len(line.lstrip("#"))
            title = line.lstrip("#").strip()
            del heading[level - 1 :]
            heading.append(title)
            continue
        if not line.strip():
            yield from flush()
            continue
        buffer.append(line)
    yield from flush()


# ── 共用 ────────────────────────────────────────────────────────


def _write(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # `sort_keys` + `ensure_ascii=False`：位元組層級的決定性是 sha256 有意義的前提，
    # 而中文不逃脫是為了 diff 讀得懂（語料檔會被人打開看）。
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _display(path: Path) -> str:
    """印給人看的路徑。repo 外的輸出（`--input`／測試用的暫存目錄）不能讓
    `relative_to` 拋例外——那會讓一次成功的取樣以 traceback 收場。"""
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _verify(corpus_out: Path, goldenset_out: Path) -> None:
    """產出的東西必須通得過載入與交叉驗證——**在這裡炸，不要等到評測時才炸**。"""
    corpus = load_corpus(corpus_out)
    goldenset = load_goldenset(goldenset_out)
    validate(goldenset, corpus)
    print(
        f"語料 {_display(corpus_out)}：{len(corpus.passages)} 段 "
        f"sha256={corpus.sha256[:12]}…\n"
        f"題組 {_display(goldenset_out)}：{len(goldenset.questions)} 題 "
        f"sha256={goldenset.sha256[:12]}…\n"
        "※ 語料或題組變動後，既有的 baseline 報告全部失效，需重跑 make eval-retrieval"
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="產生評測用的語料與題組（決定性取樣）")
    sub = parser.add_subparsers(dest="source", required=True)

    drcd = sub.add_parser("drcd", help="公開題組（DRCD，CC BY-SA 3.0）")
    drcd.add_argument(
        "--input", type=Path, nargs="*", default=None, help="原始 JSON；預設自動下載 dev+test"
    )
    # 120 而不是剛好 100：DoD 的下限是 100，而留一點餘裕才不會在剔掉幾題壞資料之後
    # 掉到線下（`test_golden_set.py` 會紅）。
    drcd.add_argument("--questions", type=int, default=120)
    drcd.add_argument("--passages", type=int, default=1200)

    docs = sub.add_parser("docs", help="自家文件的段落快照（手寫題的語料）")
    docs.add_argument("--source-dir", type=Path, default=REPO_ROOT / "docs" / "plan")
    docs.add_argument("--per-file", type=int, default=40)
    docs.add_argument("--limit", type=int, default=400)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.source == "drcd":
        sources = args.input or [
            _download(f"{_DRCD_BASE}/{name}", CACHE_DIR / name) for name in DRCD_FILES
        ]
        sample_drcd(
            sources=sources,
            questions=args.questions,
            passages=args.passages,
            corpus_out=CORPUS_DIR / "drcd.jsonl",
            goldenset_out=GOLDENSET_DIR / "drcd.jsonl",
        )
    else:
        sample_docs(
            source_dir=args.source_dir,
            per_file=args.per_file,
            limit=args.limit,
            corpus_out=CORPUS_DIR / "lumina_docs.jsonl",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

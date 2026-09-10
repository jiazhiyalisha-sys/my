#!/usr/bin/env python3
"""note のダッシュボードCSVを読み込み、「一番売れている記事」を多角的に集計する。

対応する入力（data/ に置いた CSV を自動判別）:
  - 売上系   … 販売履歴 / 売上管理からの書き出し（金額・販売数・価格などを含む）
  - アクセス系 … ダッシュボードのアクセス状況（ビュー・スキ・コメントを含む）

列名は表記ゆれを吸収して自動対応する。文字コードは utf-8-sig / cp932 / utf-8 を順に試す。

使い方:
    python3 analyze_note_sales.py                  # data/ 配下の CSV を全部読む
    python3 analyze_note_sales.py a.csv b.csv      # ファイルを直接指定
    python3 analyze_note_sales.py --min-views 300  # 転換率ランキングのPV下限を変える
"""

from __future__ import annotations

import argparse
import csv
import glob
import io
import os
import re
import sys
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field

ENCODINGS = ("utf-8-sig", "cp932", "utf-8")

# 列の役割 -> 見出しに含まれていたら採用するキーワード（優先度順）
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "title": ("記事タイトル", "コンテンツ名", "商品名", "記事名", "タイトル", "title", "記事"),
    "kind": ("種類", "商品種別", "コンテンツ種別", "タイプ", "kind", "type"),
    "date": ("日時", "購入日", "販売日", "売上日", "決済日", "日付", "date"),
    "amount": ("売上金額", "販売金額", "売上", "金額", "合計", "amount", "sales"),
    "sales_count": ("販売数", "購入数", "販売件数", "購入件数", "個数", "数量", "count"),
    "price": ("販売価格", "価格", "単価", "price"),
    "fee": ("プラットフォーム利用料", "決済手数料", "手数料", "fee"),
    "net": ("振込金額", "振込額", "受取額", "純売上", "net"),
    "views": ("全体ビュー", "ビュー数", "ビュー", "閲覧数", "view", "pv"),
    "likes": ("スキ数", "スキ", "いいね", "like"),
    "comments": ("コメント数", "コメント", "comment"),
}

# 記事以外の売上（サポート等）を既定で除外するためのキーワード
NON_ARTICLE_KINDS = ("サポート", "支援", "チップ", "メンバーシップ", "定期購読")


def normalize_header(value: str) -> str:
    """見出しの表記ゆれ（全角/半角・空白・括弧）をならす。"""
    text = unicodedata.normalize("NFKC", value or "").strip().lower()
    return re.sub(r"[\s　\"'()\[\]【】]", "", text)


def normalize_title(value: str) -> str:
    """記事タイトルの突合用キー。表示には元の文字列を使う。"""
    text = unicodedata.normalize("NFKC", value or "").strip()
    return re.sub(r"\s+", " ", text).lower()


def parse_number(value: str) -> float | None:
    """「¥1,200」「1,200円」「1200」などを数値にする。空欄や非数値は None。"""
    if value is None:
        return None
    text = unicodedata.normalize("NFKC", str(value)).strip()
    if not text:
        return None
    text = re.sub(r"[¥￥,円件人回\s]", "", text)
    text = text.replace("△", "-").replace("▲", "-")
    if text in ("", "-", "--"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def read_rows(path: str) -> tuple[list[str], list[dict[str, str]]]:
    """CSV を読み、（見出し行, 行データ）を返す。見出し前の説明行は読み飛ばす。"""
    raw = None
    for encoding in ENCODINGS:
        try:
            with open(path, "r", encoding=encoding, newline="") as handle:
                raw = handle.read()
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if raw is None:
        raise SystemExit(f"文字コードを判別できませんでした: {path}")

    table = [row for row in csv.reader(io.StringIO(raw)) if any(cell.strip() for cell in row)]
    if not table:
        return [], []

    # 先頭10行のうち、既知の列名を最も多く含む行を見出しとみなす
    def header_score(row: list[str]) -> int:
        seen = {normalize_header(cell) for cell in row}
        return sum(
            1
            for keywords in COLUMN_ALIASES.values()
            if any(any(normalize_header(k) in cell for cell in seen if cell) for k in keywords)
        )

    limit = min(10, len(table))
    header_index = max(range(limit), key=lambda i: (header_score(table[i]), -i))
    header = table[header_index]
    rows = [dict(zip(header, row)) for row in table[header_index + 1 :]]
    return header, rows


def map_columns(header: list[str]) -> dict[str, str]:
    """役割 -> 実際の列名 の対応を作る。1つの列が2役を兼ねないようにする。

    「振込金額」が amount（"金額"で部分一致）ではなく net（"振込金額"で完全一致）に
    割り当たるよう、候補を全役割まとめて「一致の具体性」順に並べてから確定させる。
    """
    normalized = {column: normalize_header(column) for column in header if column}
    candidates: list[tuple[int, int, int, str, str]] = []
    for role, keywords in COLUMN_ALIASES.items():
        for keyword in keywords:
            key = normalize_header(keyword)
            if not key:
                continue
            for column, norm in normalized.items():
                if key not in norm:
                    continue
                # 完全一致 > 長いキーワードでの一致 > 見出しが短いもの
                candidates.append((0 if norm == key else 1, -len(key), len(norm), role, column))

    mapping: dict[str, str] = {}
    taken: set[str] = set()
    for _, _, _, role, column in sorted(candidates):
        if role in mapping or column in taken:
            continue
        mapping[role] = column
        taken.add(column)
    return mapping


@dataclass
class Article:
    title: str
    sales_count: float = 0.0
    amount: float = 0.0
    fee: float = 0.0
    net: float = 0.0
    prices: list[float] = field(default_factory=list)
    views: float = 0.0
    likes: float = 0.0
    comments: float = 0.0
    has_sales_data: bool = False
    has_access_data: bool = False

    @property
    def avg_price(self) -> float | None:
        if self.prices:
            return sum(self.prices) / len(self.prices)
        if self.sales_count > 0 and self.amount > 0:
            return self.amount / self.sales_count
        return None

    @property
    def conversion(self) -> float | None:
        """PV あたりの購入率。PV が無い記事は None。"""
        if self.views > 0 and self.has_sales_data:
            return self.sales_count / self.views
        return None

    @property
    def like_rate(self) -> float | None:
        if self.views > 0:
            return self.likes / self.views
        return None


@dataclass
class LoadReport:
    sales_files: list[str] = field(default_factory=list)
    access_files: list[str] = field(default_factory=list)
    skipped_files: list[tuple[str, str]] = field(default_factory=list)
    excluded_kinds: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    monthly: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    monthly_count: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    access_title_files: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))


def extract_month(value: str) -> str | None:
    text = unicodedata.normalize("NFKC", value or "")
    match = re.search(r"(\d{4})[-/年.](\d{1,2})", text)
    if not match:
        return None
    return f"{match.group(1)}-{int(match.group(2)):02d}"


def load(paths: list[str], include_support: bool) -> tuple[dict[str, Article], LoadReport]:
    articles: dict[str, Article] = {}
    report = LoadReport()

    def get(key: str) -> Article:
        return articles.setdefault(key, Article(title=key))

    for path in sorted(paths):
        header, rows = read_rows(path)
        if not rows:
            report.skipped_files.append((path, "データ行がありません"))
            continue
        columns = map_columns(header)
        if "title" not in columns:
            report.skipped_files.append((path, "記事タイトルに相当する列が見つかりません"))
            continue

        is_sales = any(role in columns for role in ("amount", "sales_count", "price"))
        is_access = "views" in columns
        if not is_sales and not is_access:
            report.skipped_files.append((path, "売上・アクセスいずれの指標列も見つかりません"))
            continue
        if is_sales:
            report.sales_files.append(path)
        if is_access:
            report.access_files.append(path)

        # 明示的な販売数列が無い売上ファイルは「1行＝1件の購入」とみなす
        row_is_transaction = is_sales and "sales_count" not in columns

        for row in rows:
            raw_title = (row.get(columns["title"]) or "").strip()
            if not raw_title:
                continue
            key = normalize_title(raw_title)
            if not key:
                continue

            amount = parse_number(row.get(columns["amount"])) if "amount" in columns else None
            count = parse_number(row.get(columns["sales_count"])) if "sales_count" in columns else None
            price = parse_number(row.get(columns["price"])) if "price" in columns else None

            if is_sales:
                kind = (row.get(columns["kind"]) or "").strip() if "kind" in columns else ""
                if kind and not include_support and any(k in kind for k in NON_ARTICLE_KINDS):
                    report.excluded_kinds[kind] += amount if amount is not None else (price or 0.0)
                    continue

            article = get(key)
            if article.title == key:
                article.title = raw_title  # 表示は元の見た目を優先

            if is_sales:
                article.has_sales_data = True

                # この行が表す販売件数。明示列が無ければ「1行＝1件の購入」とみなす。
                if count is not None:
                    row_count = count
                elif row_is_transaction and (amount is None or amount > 0):
                    row_count = 1.0
                else:
                    row_count = 0.0

                # この行の売上。金額列が無ければ 価格×件数 で補う（手数料引き後の
                # 振込額ではなく、販売価格ベースの総額を売上とする）。
                if amount is not None:
                    row_amount = amount
                elif price is not None:
                    row_amount = price * row_count
                else:
                    row_amount = 0.0

                article.sales_count += row_count
                article.amount += row_amount
                if price is not None and price > 0:
                    article.prices.append(price)
                if "fee" in columns:
                    article.fee += parse_number(row.get(columns["fee"])) or 0.0
                if "net" in columns:
                    article.net += parse_number(row.get(columns["net"])) or 0.0
                if "date" in columns:
                    month = extract_month(row.get(columns["date"]) or "")
                    if month:
                        report.monthly[month] += row_amount
                        report.monthly_count[month] += row_count

            if is_access:
                article.has_access_data = True
                report.access_title_files[key].add(os.path.basename(path))
                for role, attr in (("views", "views"), ("likes", "likes"), ("comments", "comments")):
                    if role in columns:
                        value = parse_number(row.get(columns[role]))
                        if value is not None:
                            setattr(article, attr, getattr(article, attr) + value)

    return articles, report


def yen(value: float | None) -> str:
    if value is None:
        return "—"
    return f"¥{value:,.0f}"


def pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.2f}%"


def num(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.0f}"


def percentile_scores(values: dict[str, float]) -> dict[str, float]:
    """値が大きいほど 100 に近づく 0-100 のスコア。同値は同スコア。"""
    if not values:
        return {}
    ordered = sorted(values.items(), key=lambda item: item[1])
    total = len(ordered)
    if total == 1:
        return {ordered[0][0]: 100.0}
    scores: dict[str, float] = {}
    index = 0
    while index < total:
        stop = index
        while stop + 1 < total and ordered[stop + 1][1] == ordered[index][1]:
            stop += 1
        rank = (index + stop) / 2
        for position in range(index, stop + 1):
            scores[ordered[position][0]] = rank / (total - 1) * 100
        index = stop + 1
    return scores


def table(rows: list[list[str]], headers: list[str]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return lines


def build_report(articles: dict[str, Article], load_report: LoadReport, top_n: int, min_views: int) -> str:
    sold = [a for a in articles.values() if a.has_sales_data and (a.amount > 0 or a.sales_count > 0)]
    out: list[str] = ["# note 売上分析レポート", ""]

    if not sold:
        out += [
            "売上データを含む記事が見つかりませんでした。",
            "`data/` に note の売上CSVを置いて再実行してください（詳細は `data/README.md`）。",
            "",
        ]
        return "\n".join(out)

    total_amount = sum(a.amount for a in sold)
    total_count = sum(a.sales_count for a in sold)
    total_net = sum(a.net for a in sold)
    with_views = [a for a in sold if a.views > 0]

    by_amount = sorted(sold, key=lambda a: a.amount, reverse=True)
    by_count = sorted(sold, key=lambda a: a.sales_count, reverse=True)
    eligible = [a for a in with_views if a.views >= min_views]
    by_conversion = sorted(eligible, key=lambda a: a.conversion or 0, reverse=True)

    # --- 結論 -------------------------------------------------------------
    out += ["## 結論", ""]
    top = by_amount[0]
    share = top.amount / total_amount if total_amount else 0
    out.append(f"- **売上金額の1位は「{top.title}」**（{yen(top.amount)} / 全体の{share * 100:.1f}%）")
    if by_count and by_count[0] is not top:
        out.append(f"- **販売数の1位は「{by_count[0].title}」**（{num(by_count[0].sales_count)}件）— 金額1位とは別の記事")
    elif by_count:
        out.append(f"- 販売数も同じ記事が1位（{num(by_count[0].sales_count)}件）")
    if by_conversion:
        best = by_conversion[0]
        out.append(
            f"- **転換率の1位は「{best.title}」**（{pct(best.conversion)} / {num(best.views)}PV・{num(best.sales_count)}件）"
        )
    elif with_views:
        out.append(f"- 転換率: PV {min_views:,} 以上の記事が無いため判定を保留（`--min-views` で下限を調整可能）")
    else:
        out.append("- 転換率: アクセス状況CSV（ビュー数）が未投入のため算出できません")
    out.append("")

    # --- 全体サマリー -----------------------------------------------------
    out += ["## 全体サマリー", ""]
    summary = [
        ["売上合計", yen(total_amount)],
        ["販売数合計", f"{num(total_count)}件"],
        ["売上のある記事数", f"{len(sold)}本"],
        ["平均単価", yen(total_amount / total_count if total_count else None)],
        ["1記事あたり平均売上", yen(total_amount / len(sold))],
    ]
    if total_net > 0:
        summary.append(["振込ベース合計", yen(total_net)])
    if with_views:
        views_total = sum(a.views for a in with_views)
        counts_total = sum(a.sales_count for a in with_views)
        summary.append(["全体転換率", pct(counts_total / views_total if views_total else None)])
    out += table([[label, value] for label, value in summary], ["項目", "値"]) + [""]

    # --- 3つの基準 --------------------------------------------------------
    out += ["## 基準1: 売上金額ランキング", ""]
    out += table(
        [
            [
                str(i),
                a.title,
                yen(a.amount),
                num(a.sales_count),
                yen(a.avg_price),
                f"{a.amount / total_amount * 100:.1f}%",
            ]
            for i, a in enumerate(by_amount[:top_n], 1)
        ],
        ["#", "記事", "売上", "販売数", "平均単価", "構成比"],
    ) + [""]

    out += ["## 基準2: 販売数ランキング", ""]
    out += table(
        [[str(i), a.title, num(a.sales_count), yen(a.amount), yen(a.avg_price)] for i, a in enumerate(by_count[:top_n], 1)],
        ["#", "記事", "販売数", "売上", "平均単価"],
    ) + [""]

    out += [f"## 基準3: 転換率ランキング（PV {min_views:,} 以上）", ""]
    if by_conversion:
        out += table(
            [
                [str(i), a.title, pct(a.conversion), num(a.views), num(a.sales_count), yen(a.amount)]
                for i, a in enumerate(by_conversion[:top_n], 1)
            ],
            ["#", "記事", "転換率", "PV", "販売数", "売上"],
        ) + [""]
        excluded = len(with_views) - len(eligible)
        if excluded > 0:
            out += [f"※ PV {min_views:,} 未満の {excluded} 本は母数が小さく振れやすいため除外。", ""]
    else:
        out += ["アクセス状況CSV（ビュー数）が無いため算出できません。", ""]

    # --- 総合 -------------------------------------------------------------
    out += ["## 総合ランキング（3基準の合成）", ""]
    amount_scores = percentile_scores({a.title: a.amount for a in sold})
    count_scores = percentile_scores({a.title: a.sales_count for a in sold})
    conversion_scores = percentile_scores({a.title: a.conversion or 0 for a in eligible})
    composite: list[tuple[float, Article, list[str]]] = []
    for article in sold:
        parts, used = [], []
        for score_map, label in (
            (amount_scores, "売上"),
            (count_scores, "販売数"),
            (conversion_scores, "転換率"),
        ):
            if article.title in score_map:
                parts.append(score_map[article.title])
                used.append(label)
        if parts:
            composite.append((sum(parts) / len(parts), article, used))
    composite.sort(key=lambda item: item[0], reverse=True)
    out += table(
        [
            [str(i), a.title, f"{score:.1f}", yen(a.amount), num(a.sales_count), pct(a.conversion), "+".join(used)]
            for i, (score, a, used) in enumerate(composite[:top_n], 1)
        ],
        ["#", "記事", "総合スコア", "売上", "販売数", "転換率", "評価に使った指標"],
    ) + ["", "※ 各指標を0-100の相対スコアに変換し、算出できた指標の平均をとったもの。", ""]

    # --- 傾向 -------------------------------------------------------------
    priced = [a for a in sold if a.avg_price]
    if priced:
        out += ["## 価格帯別の傾向", ""]
        bands = [(0, 500), (500, 1000), (1000, 3000), (3000, 10000), (10000, float("inf"))]
        rows = []
        for low, high in bands:
            group = [a for a in priced if low <= (a.avg_price or 0) < high]
            if not group:
                continue
            group_views = sum(a.views for a in group)
            group_counts = sum(a.sales_count for a in group)
            label = f"¥{low:,}〜" if high == float("inf") else f"¥{low:,}〜{high:,}"
            rows.append(
                [
                    label,
                    f"{len(group)}本",
                    yen(sum(a.amount for a in group)),
                    num(group_counts),
                    yen(sum(a.amount for a in group) / len(group)),
                    pct(group_counts / group_views) if group_views else "—",
                ]
            )
        out += table(rows, ["価格帯", "記事数", "売上", "販売数", "1本あたり売上", "転換率"]) + [""]

    if load_report.monthly:
        out += ["## 月別推移", ""]
        rows = [
            [month, yen(load_report.monthly[month]), num(load_report.monthly_count.get(month))]
            for month in sorted(load_report.monthly)
        ]
        out += table(rows, ["年月", "売上", "販売数"]) + [""]

    # --- データ品質 -------------------------------------------------------
    out += ["## データについて", ""]
    if load_report.sales_files:
        out.append(f"- 売上CSV: {', '.join(os.path.basename(p) for p in load_report.sales_files)}")
    if load_report.access_files:
        out.append(f"- アクセスCSV: {', '.join(os.path.basename(p) for p in load_report.access_files)}")
    no_views = [a for a in sold if not a.has_access_data]
    if no_views:
        out.append(f"- PV不明のため転換率を出せない記事: {len(no_views)}本（アクセス状況CSVを追加すると解消します）")
    unsold_but_viewed = [a for a in articles.values() if a.has_access_data and not a.has_sales_data]
    if unsold_but_viewed:
        out.append(f"- アクセスCSVにのみ存在（無料記事、または売上0）: {len(unsold_but_viewed)}本")
    duplicated = [title for title, files in load_report.access_title_files.items() if len(files) > 1]
    if duplicated:
        out.append(f"- ⚠ 同じ記事が複数のアクセスCSVに含まれています（{len(duplicated)}本）。PVが二重計上されている可能性があります。")
    if load_report.excluded_kinds:
        detail = "、".join(f"{k}: {yen(v)}" for k, v in load_report.excluded_kinds.items())
        out.append(f"- 記事以外の売上として除外: {detail}（`--include-support` で集計対象にできます）")
    for path, reason in load_report.skipped_files:
        out.append(f"- ⚠ 読み飛ばし: {os.path.basename(path)}（{reason}）")
    out.append("")
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description="note の売上CSVを分析する")
    parser.add_argument("paths", nargs="*", help="CSVファイル（省略時は data/ 配下を読む）")
    parser.add_argument("--data-dir", default="data", help="CSVを探すディレクトリ（既定: data）")
    parser.add_argument("--top", type=int, default=10, help="各ランキングの表示件数（既定: 10）")
    parser.add_argument("--min-views", type=int, default=100, help="転換率ランキングのPV下限（既定: 100）")
    parser.add_argument("--include-support", action="store_true", help="サポート等の記事以外の売上も含める")
    parser.add_argument("--out", default="reports/note_sales_report.md", help="レポートの出力先")
    args = parser.parse_args()

    paths = args.paths or sorted(glob.glob(os.path.join(args.data_dir, "**", "*.csv"), recursive=True))
    if not paths:
        print(f"CSVが見つかりません: {args.data_dir}/ に note のCSVを置いてください。", file=sys.stderr)
        print("置き方は data/README.md を参照。", file=sys.stderr)
        return 1

    articles, load_report = load(paths, include_support=args.include_support)
    report = build_report(articles, load_report, top_n=args.top, min_views=args.min_views)

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        handle.write(report)

    print(report)
    print(f"\n→ レポートを書き出しました: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

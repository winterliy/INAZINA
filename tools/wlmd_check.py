"""Validate Wide-Learning WLMD records (docs/wide-learning, see WLMD.md).

    python3 tools/wlmd_check.py                 # format, sources and verdict rules
    python3 tools/wlmd_check.py --against HEAD  # + append-only check against a git ref
"""
import argparse
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = REPO / "docs" / "wide-learning"
THREADS = {"threads/cthread-claude.md": ("C", "CLAUDE"),
           "threads/gthread-codex.md": ("G", "CODEX")}
APPEND_ONLY = (*THREADS, "registry.md")
AUTHOR_PREFIX = {author: prefix for prefix, author in THREADS.values()}
REGISTRY_RANGES = {"USER": range(0, 100), "CLAUDE": range(100, 200), "CODEX": range(200, 300)}

HEADER = re.compile(r"^### \[([A-Z])-(\d{2,3})\] (.+?)\s*$")
FIELD = re.compile(r"^- ([a-z-]+):[ \t]?(.*)$")
THREAD_TITLE = re.compile(r"^(CLAUDE|CODEX) · (\d{4}-\d{2}-\d{2}) · (T-\d{2})$")
ENTRY_REF = re.compile(r"\[([CG]-\d{3})\]")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

TIERS = ("S1", "S2", "S3")
TYPES = ("claim", "question", "review", "correction", "summary")
STATUSES = ("proposed", "verified", "disputed", "simulator-assumption", "rejected")
SETTLED = ("verified", "simulator-assumption")
VERDICTS = ("agree", "dispute", "needs-source", "partial")
CONFIDENCE = ("high", "medium", "low")
TOPIC_STATUSES = ("draft", "approved", "in-review", "done", "parked")
APPLY_STATUSES = ("proposed", "approved", "applied", "rejected")
REQUIRED = {
    "claim": ("claim", "sources", "tier", "confidence", "status", "laplace"),
    "correction": ("re", "claim", "sources", "tier", "confidence", "status"),
    "review": ("re", "verdict", "checked"),
    "question": (),
    "summary": (),
}


@dataclass
class Entry:
    id: str
    title: str
    path: str
    line: int
    fields: dict = field(default_factory=dict)

    @property
    def number(self):
        return int(self.id[2:])

    def ids(self, key):
        return [item.strip().split()[0] for item in self.fields.get(key, "").split(";")
                if item.strip() and item.strip().lower() != "none"]


def parse(text, path):
    """Return the ### [X-NNN] entries of a file, ignoring fenced examples."""
    entries, current, last_key, fenced = [], None, None, False
    for number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        header = HEADER.match(line)
        if header:
            current = Entry(f"{header[1]}-{header[2]}", header[3], path, number)
            entries.append(current)
            last_key = None
        elif current and (match := FIELD.match(line)):
            last_key = match[1]
            current.fields[last_key] = match[2].strip()
        elif current and last_key and line.startswith("  ") and line.strip():
            current.fields[last_key] += " " + line.strip()
        elif line.startswith("#"):
            current, last_key = None, None
    return entries


def outside_fences(text):
    fenced, kept = False, []
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif not fenced:
            kept.append(line)
    return "\n".join(kept)


class Checker:
    def __init__(self, root):
        self.root = Path(root)
        self.errors = []

    def error(self, entry_or_path, message):
        where = (f"{entry_or_path.path}:{entry_or_path.line} [{entry_or_path.id}]"
                 if isinstance(entry_or_path, Entry) else str(entry_or_path))
        self.errors.append(f"{where}: {message}")

    def read(self, relative):
        path = self.root / relative
        if not path.exists():
            self.error(relative, "파일이 없습니다.")
            return ""
        return path.read_text(encoding="utf-8")

    # ------------------------------------------------------------------
    def run(self, against=None):
        self.sources = self.check_registry(parse(self.read("registry.md"), "registry.md"))
        self.topics = self.check_topics(parse(self.read("topics.md"), "topics.md"))
        self.entries = {}
        for relative, (prefix, author) in THREADS.items():
            self.check_thread(parse(self.read(relative), relative), prefix, author)
        for entry in self.entries.values():
            self.check_entry(entry)
        self.check_apply_queue(parse(self.read("apply-queue.md"), "apply-queue.md"))
        self.check_summary(self.read("wide-learning.md"))
        if against:
            self.check_append_only(against)
        return self.errors

    def check_registry(self, entries):
        sources, urls = {}, {}
        for entry in entries:
            if not re.match(r"^S-\d{3}$", entry.id):
                self.error(entry, "출처 ID는 S-NNN 형식이어야 합니다.")
                continue
            if entry.id in sources:
                self.error(entry, "중복된 출처 ID입니다.")
            sources[entry.id] = entry
            for key in ("url", "publisher", "tier", "accessed", "added-by"):
                if not entry.fields.get(key):
                    self.error(entry, f"필수 필드 {key}가 없습니다.")
            if entry.fields.get("tier") and entry.fields["tier"] not in TIERS:
                self.error(entry, "tier는 S1·S2·S3 중 하나여야 합니다(X 자료는 등록 금지).")
            url = entry.fields.get("url", "")
            if url and not re.match(r"^https?://", url):
                self.error(entry, "url은 http(s) 주소여야 합니다.")
            if url in urls:
                self.error(entry, f"같은 URL이 {urls[url]}에 이미 있습니다. 기존 ID를 쓰세요.")
            urls.setdefault(url, entry.id)
            if entry.fields.get("accessed") and not DATE.match(entry.fields["accessed"]):
                self.error(entry, "accessed는 YYYY-MM-DD 형식이어야 합니다.")
            allowed = REGISTRY_RANGES.get(entry.fields.get("added-by", ""))
            if allowed is None:
                self.error(entry, "added-by는 USER·CLAUDE·CODEX 중 하나여야 합니다.")
            elif entry.number not in allowed:
                self.error(entry, f"{entry.fields['added-by']}의 출처 ID 범위가 아닙니다.")
        return sources

    def check_topics(self, entries):
        topics = {}
        for entry in entries:
            if not re.match(r"^T-\d{2}$", entry.id):
                self.error(entry, "주제 ID는 T-NN 형식이어야 합니다.")
                continue
            if entry.id in topics:
                self.error(entry, "중복된 주제 ID입니다.")
            topics[entry.id] = entry
            for key in ("question", "laplace", "status", "owner"):
                if not entry.fields.get(key):
                    self.error(entry, f"필수 필드 {key}가 없습니다.")
            if entry.fields.get("status") not in TOPIC_STATUSES:
                self.error(entry, f"status는 {', '.join(TOPIC_STATUSES)} 중 하나여야 합니다.")
        return topics

    def check_thread(self, entries, prefix, author):
        previous = 0
        for entry in entries:
            if not re.match(rf"^{prefix}-\d{{3}}$", entry.id):
                self.error(entry, f"이 파일의 항목 ID는 {prefix}-NNN이어야 합니다.")
                continue
            if entry.number <= previous:
                self.error(entry, "항목 번호는 중복 없이 차례로 커져야 합니다.")
            previous = max(previous, entry.number)
            title = THREAD_TITLE.match(entry.title)
            if not title:
                self.error(entry, "머리줄은 '작성자 · YYYY-MM-DD · T-NN' 형식이어야 합니다.")
                continue
            if title[1] != author:
                self.error(entry, f"이 파일의 작성자는 {author}입니다.")
            entry.author, entry.topic = title[1], title[3]
            self.entries[entry.id] = entry

    # ------------------------------------------------------------------
    def root_of(self, entry):
        seen = set()
        while entry.fields.get("type") == "correction" and entry.id not in seen:
            seen.add(entry.id)
            target = self.entries.get(entry.fields.get("re", ""))
            if not target:
                break
            entry = target
        return entry

    def chain(self, root):
        """The root claim and every correction that (transitively) amends it."""
        return [entry for entry in self.entries.values() if self.root_of(entry) is root]

    def final_status(self, entry_id):
        entry = self.entries.get(entry_id)
        if not entry:
            return None
        root = self.root_of(entry)
        amendments = [item for item in self.chain(root)
                      if item.fields.get("type") in ("claim", "correction")
                      and item.author == root.author]
        latest = max(amendments, key=lambda item: item.number, default=root)
        return latest.fields.get("status")

    def check_entry(self, entry):
        kind = entry.fields.get("type")
        if kind not in TYPES:
            self.error(entry, f"type은 {', '.join(TYPES)} 중 하나여야 합니다.")
            return
        for key in REQUIRED[kind]:
            if not entry.fields.get(key):
                self.error(entry, f"{kind} 항목에 필수 필드 {key}가 없습니다.")
        topic = self.topics.get(entry.topic)
        if not topic:
            self.error(entry, f"topics.md에 없는 주제 {entry.topic}입니다.")
        elif topic.fields.get("status") == "draft":
            self.error(entry, f"{entry.topic}는 아직 승인되지 않은(draft) 주제입니다.")
        target = None
        if entry.fields.get("re"):
            target = self.entries.get(entry.fields["re"])
            if not target:
                self.error(entry, f"re 대상 {entry.fields['re']}가 없습니다.")
        if kind in ("claim", "correction"):
            self.check_claim(entry)
        if kind == "correction" and target and target.author != entry.author:
            self.error(entry, "correction은 자기 항목에만 쓸 수 있습니다. 상대 주장에는 review를 쓰세요.")
        if kind == "review":
            if entry.fields.get("verdict") not in VERDICTS:
                self.error(entry, f"verdict는 {', '.join(VERDICTS)} 중 하나여야 합니다.")
            for source_id in entry.ids("checked"):
                if source_id not in self.sources:
                    self.error(entry, f"checked의 {source_id}가 registry.md에 없습니다.")
            if target and target.author == entry.author:
                self.error(entry, "자기 주장은 검토할 수 없습니다.")

    def check_claim(self, entry):
        sources = entry.ids("sources")
        tiers = []
        for source_id in sources:
            source = self.sources.get(source_id)
            if not source:
                self.error(entry, f"출처 {source_id}가 registry.md에 없습니다.")
            elif source.fields.get("tier") in TIERS:
                tiers.append(source.fields["tier"])
        best = min(tiers, default=None)
        declared = entry.fields.get("tier")
        if declared and declared not in TIERS:
            self.error(entry, "tier는 S1·S2·S3 중 하나여야 합니다.")
        elif declared and best and declared != best:
            self.error(entry, f"tier는 인용 출처의 최고 등급({best})과 같아야 합니다.")
        if entry.fields.get("confidence") and entry.fields["confidence"] not in CONFIDENCE:
            self.error(entry, "confidence는 high·medium·low 중 하나여야 합니다.")
        status = entry.fields.get("status")
        if status and status not in STATUSES:
            self.error(entry, f"status는 {', '.join(STATUSES)} 중 하나여야 합니다.")
        laplace = entry.fields.get("laplace")
        if laplace and not all(part.strip().isdigit() and 1 <= int(part) <= 12
                               for part in laplace.split(",")):
            self.error(entry, "laplace는 1~12 장 번호여야 합니다.")
        if status == "verified":
            if "S1" not in tiers:
                self.error(entry, "verified에는 S1 출처가 하나 이상 필요합니다.")
            root = self.root_of(entry)
            targets = {item.id for item in self.chain(root)}
            if not any(review.fields.get("type") == "review"
                       and review.author != entry.author
                       and review.fields.get("re") in targets
                       and review.fields.get("verdict") == "agree"
                       and review.ids("checked")
                       for review in self.entries.values()):
                self.error(entry, "verified에는 상대 CLI가 출처를 직접 확인한 agree 검토가 필요합니다.")

    # ------------------------------------------------------------------
    def check_apply_queue(self, entries):
        for entry in entries:
            for key in ("basis", "laplace", "change", "reuse", "impact", "rollback",
                        "tests", "status", "commit"):
                if not entry.fields.get(key):
                    self.error(entry, f"필수 필드 {key}가 없습니다.")
            if entry.fields.get("status") not in APPLY_STATUSES:
                self.error(entry, f"status는 {', '.join(APPLY_STATUSES)} 중 하나여야 합니다.")
            for basis in entry.ids("basis"):
                status = self.final_status(basis)
                if status is None:
                    self.error(entry, f"근거 항목 {basis}가 없습니다.")
                elif status not in SETTLED:
                    self.error(entry, f"근거 {basis}의 최종 상태가 {status}입니다(verified·simulator-assumption만 가능).")
            if entry.fields.get("status") == "applied" and entry.fields.get("commit") in ("", "-"):
                self.error(entry, "applied 항목에는 commit을 적어야 합니다.")

    def check_summary(self, text):
        for entry_id in sorted(set(ENTRY_REF.findall(outside_fences(text)))):
            status = self.final_status(entry_id)
            if status is None:
                self.error("wide-learning.md", f"없는 항목 {entry_id}를 인용했습니다.")
            elif status not in SETTLED:
                self.error("wide-learning.md", f"{entry_id}는 {status} 상태라 통합본에 넣을 수 없습니다.")

    def check_append_only(self, ref):
        toplevel = Path(subprocess.run(
            ["git", "-C", str(self.root), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True).stdout.strip())
        for relative in APPEND_ONLY:
            path = (self.root / relative).resolve()
            spec = f"{ref}:{path.relative_to(toplevel.resolve()).as_posix()}"
            result = subprocess.run(["git", "-C", str(toplevel), "show", spec],
                                    capture_output=True, text=True)
            if result.returncode != 0 or not path.exists():
                continue
            if not appended_only(result.stdout, path.read_text(encoding="utf-8")):
                self.error(relative, f"{ref} 이후 기존 내용이 바뀌었습니다. 덧붙이기만 할 수 있습니다.")

    def summary(self):
        lines = []
        for topic_id, topic in sorted(self.topics.items()):
            roots = [entry for entry in self.entries.values()
                     if entry.topic == topic_id and entry.fields.get("type") == "claim"]
            counts = {}
            for root in roots:
                status = self.final_status(root.id)
                counts[status] = counts.get(status, 0)+1
            detail = ", ".join(f"{key} {value}" for key, value in sorted(counts.items())) or "주장 없음"
            lines.append(f"{topic_id} [{topic.fields.get('status')}] {topic.title} — {detail}")
        return lines


def appended_only(old, new):
    return new.startswith(old.rstrip("\n"))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Wide-Learning WLMD 기록 검사")
    parser.add_argument("--root", default=str(DEFAULT_ROOT))
    parser.add_argument("--against", help="덧붙이기만 했는지 비교할 git ref (예: HEAD, origin/main)")
    args = parser.parse_args(argv)
    checker = Checker(args.root)
    errors = checker.run(args.against)
    for line in checker.summary():
        print(line)
    if errors:
        print(f"\n오류 {len(errors)}건:")
        for message in errors:
            print(f"  - {message}")
        return 1
    print("\n✅ WLMD 검사 통과")
    return 0


if __name__ == "__main__":
    sys.exit(main())

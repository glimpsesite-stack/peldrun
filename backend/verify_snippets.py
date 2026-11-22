import sys, re, logging, asyncio, tempfile
sys.path.insert(0, '.'); logging.disable(logging.CRITICAL)

# ---------- 1) blank query must still return recents -------------
from pathlib import Path
from peldrun.memory.domain.entry import MemoryEntry, MemoryProvenance
from peldrun.memory.domain.enums import MemoryType, MemoryScope, MemorySourceType
from peldrun.memory.providers.sqlite_fts import SqliteFtsProvider
from peldrun.memory.domain.scope import resolve_scope_refs
from omweb.memory.storage.normalizer import normalize_for_storage

async def blank():
    prov = SqliteFtsProvider(db_path=Path(tempfile.mkdtemp())/"m.db"); await prov.open()
    for t in ["a one two", "b three four"]:
        e = MemoryEntry.create(content=t, memory_type=MemoryType.SEMANTIC, scope_type=MemoryScope.USER, scope_id="default",
            provenance=MemoryProvenance(source_type=MemorySourceType.EXPLICIT_USER), normalized_content=normalize_for_storage(t))
        await prov.create(e)
    refs = resolve_scope_refs(chat_id="c", project_id="p", user_id="default")
    for q in ["", "   ", "؟!", "thank you"]:
        r = await prov.search_ranked(q, refs, 5, match_any=True)
        print(f"query={q!r:12} -> {len(r)} results")
    await prov.close()
print("--- 1) blank/punct query still lists recents; stopword-only returns none")
asyncio.run(blank())

# ---------- 2) minimizer: proposed extra patterns ---------------
def luhn(num):
    ds = [int(c) for c in re.sub(r"\D", "", num)][::-1]
    s = sum(d if i % 2 == 0 else (d*2 - 9 if d*2 > 9 else d*2) for i, d in enumerate(ds))
    return len(ds) >= 13 and s % 10 == 0

PATTERNS = [
  (re.compile(r"\bsk-(?:[A-Za-z0-9]+-){0,3}[A-Za-z0-9_-]{20,}"), "[REDACTED]"),
  (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "[REDACTED]"),
  (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "[REDACTED]"),
  (re.compile(r"(\b[a-z][a-z0-9+.-]*://[^/\s:@]+:)([^@\s/]+)(@)", re.I), r"\1[REDACTED]\3"),
  # Arabic: «كلمة المرور/السر», «الرقم السري», «الباسورد» ... then (هي|هو|:|=) value
  (re.compile(r"((?:كلم[ةه]\s+(?:ال)?(?:مرور|سر)|الرقم\s+السري|(?:ال)?باس?ورد)[^\n:=]{0,30}?(?:\s(?:هي|هو)\s+|\s*[:=]\s*))(\S+)"), r"\1[REDACTED]"),
]
CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")
def minimize(t):
    for rx, rep in PATTERNS: t = rx.sub(rep, t)
    return CARD.sub(lambda m: "[REDACTED]" if luhn(m.group()) else m.group(), t)

print("\n--- 2) proposed redaction patterns")
cases = [
 ("كلمة المرور الخاصة بي هي hunter2", True), ("كلمة السر: Tr0ub4dor&3", True), ("كلمه السر هي abc123", True),
 ("الباسورد = x9y8z7", True),
 ("sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcdef", True), ("sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789", True),
 ("AWS key AKIAIOSFODNN7EXAMPLE", True), ("postgres://admin:s3cret@db.example.com:5432/app", True),
 ("-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----", True),
 ("رقم بطاقتي 4111 1111 1111 1111", True),
 # false-positive guards (must stay UNCHANGED)
 ("رقم هاتفي 0501234567", False), ("تاريخ الميلاد 2026-11-21", False), ("أحب كلمة السر في الألعاب", False),
 ("I use skikit-learn and ski-resort-guides daily", False), ("https://example.com/path", False),
]
bad = 0
for t, should in cases:
    out = minimize(t); changed = out != t
    ok = changed == should; bad += (not ok)
    print(f"{'OK ' if ok else 'BAD'} {'redact' if should else 'keep  '} | {t[:46]!r:50} -> {out[:44]!r}")
print("failures:", bad)

# ---------- 3) Arabic: fold + stem on BOTH sides ---------------
from nltk.stem.isri import ISRIStemmer
st = ISRIStemmer()
def fold(t):
    t = normalize_for_storage(t)
    return t.replace("ة", "ه")
def stems(text):
    return {st.stem(w) for w in re.findall(r"\w+", fold(text)) if len(w) > 1}
print("\n--- 3) stem overlap (query vs stored) with ISRI + taa-marbuta folding")
pairs = [("ما هي قهوتي المفضلة؟", "أحب القهوة السوداء بدون سكر"),
         ("في أي مدينة أعمل؟", "اسمي أحمد وأعمل مهندس برمجيات في فرانكفورت"),
         ("ما اسم قطتي؟", "لدي قطة اسمها لولو"),
         ("ما هي قاعدة البيانات التي اخترناها؟", "قررنا استخدام PostgreSQL لقاعدة بيانات المشروع")]
for q, d in pairs:
    print(f"{q!r:42} ∩ -> {sorted(stems(q) & stems(d))}   (q={sorted(stems(q))})")

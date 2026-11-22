import sys, asyncio, tempfile, logging
sys.path.insert(0, '.')
logging.disable(logging.CRITICAL)
from pathlib import Path
from peldrun.memory.domain.entry import MemoryEntry, MemoryProvenance
from peldrun.memory.domain.enums import MemoryType, MemoryScope, MemorySourceType
from peldrun.memory.domain.settings import MemorySettings
from peldrun.memory.domain.scope import resolve_scope_refs
from peldrun.memory.services.context_builder import build_memory_context
from peldrun.memory.services.router import Route, RouteDecision
from peldrun.memory.providers.sqlite_fts import SqliteFtsProvider
from omweb.memory.storage.normalizer import normalize_for_storage

FACTS = [
    ("أحب القهوة السوداء بدون سكر", MemoryType.PREFERENCE),
    ("اسمي أحمد وأعمل مهندس برمجيات في فرانكفورت", MemoryType.SEMANTIC),
    ("قررنا استخدام PostgreSQL لقاعدة بيانات المشروع", MemoryType.DECISION),
    ("I prefer dark mode in all editors", MemoryType.PREFERENCE),
    ("The deployment target is Hetzner with Docker Compose", MemoryType.SEMANTIC),
    ("لدي قطة اسمها لولو", MemoryType.SEMANTIC),
    ("أستخدم لغة بايثون في معظم مشاريعي", MemoryType.SEMANTIC),
]

async def main():
    tmp = tempfile.mkdtemp()
    prov = SqliteFtsProvider(db_path=Path(tmp) / "m.db")
    await prov.open()
    for text, mt in FACTS:
        e = MemoryEntry.create(
            content=text, memory_type=mt, scope_type=MemoryScope.USER, scope_id="default",
            provenance=MemoryProvenance(source_type=MemorySourceType.EXPLICIT_USER),
            normalized_content=normalize_for_storage(text),
        )
        await prov.write(e) if hasattr(prov, "write") else await prov.create(e)
    settings = MemorySettings()
    print("min_relevance_score =", settings.min_relevance_score, "| default_limit =", settings.default_limit,
          "| max_tokens =", settings.token_budget.max_tokens)
    refs = resolve_scope_refs(chat_id="c1", project_id="p1", user_id="default")
    route = Route(decision=RouteDecision.DIRECTED, reason="always_retrieve_setting")
    queries = [
        "thank you", "ok see you next time", "hello", "شكرا لك", "كيف حالك اليوم",
        "ما هي قهوتي المفضلة؟", "what coffee do I like", "ما اسم قطتي؟", "في أي مدينة أعمل؟",
        "which database did we pick", "ما هي قاعدة البيانات التي اخترناها؟", "write me a python function to sort a list",
    ]
    for q in queries:
        ctx = await build_memory_context(query=q, scope_refs=refs, route=route, provider=prov,
                                         settings=settings, allow_recency_fallback=False, match_any=True)
        got = [m.content[:38] for m in ctx.memories]
        print(f"{q!r:48} -> {len(got)} injected {got}")
    await prov.close()
asyncio.run(main())

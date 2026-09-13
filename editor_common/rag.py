"""RAG (retrieval-augmented generation) index over story memory, backed by
Qdrant with real embeddings (via `ollama.embed`). Shared by both editors
except for the collection name and how the URL/embedder are resolved.
"""
import logging
from typing import Awaitable, Callable

from qdrant_client import QdrantClient, models

logger = logging.getLogger(__name__)

EmbedFn = Callable[[list[str]], Awaitable[list[list[float]]]]


class RagStore:
    def __init__(self, get_qdrant_url: Callable[[], str], embed: EmbedFn, collection: str):
        self._get_qdrant_url = get_qdrant_url
        self._embed = embed
        self.collection = collection

    def client(self) -> QdrantClient:
        return QdrantClient(url=self._get_qdrant_url())

    def ensure(self, size: int) -> None:
        c = self.client()
        names = [x.name for x in c.get_collections().collections]
        if self.collection not in names:
            c.create_collection(
                collection_name=self.collection,
                vectors_config=models.VectorParams(size=size, distance=models.Distance.COSINE),
            )

    async def index(self, chunks: list[dict]) -> int:
        if not chunks:
            return 0
        vs = await self._embed([x['text'] for x in chunks])
        self.ensure(len(vs[0]))
        self.client().upsert(
            collection_name=self.collection,
            points=[models.PointStruct(id=x['id'], vector=v, payload=x) for x, v in zip(chunks, vs)],
        )
        return len(chunks)

    async def search(self, project_id: int, q: str, limit: int = 8) -> list[dict]:
        v = (await self._embed([q]))[0]
        self.ensure(len(v))
        r = self.client().query_points(
            collection_name=self.collection,
            query=v,
            query_filter=models.Filter(must=[models.FieldCondition(key='project_id', match=models.MatchValue(value=project_id))]),
            with_payload=True,
            limit=limit,
        )
        return [p.payload for p in r.points]

    async def search_all_projects(self, q: str, limit: int = 8) -> list[dict]:
        v = (await self._embed([q]))[0]
        self.ensure(len(v))
        r = self.client().query_points(collection_name=self.collection, query=v, with_payload=True, limit=limit)
        return [p.payload for p in r.points]

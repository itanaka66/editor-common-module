"""Cheap, dependency-free "vectorization" (a hash spread into a unit
vector) used to give the MVP scene-similarity search something to index
before real embeddings are wired up. Shared verbatim by both editors except
for the Qdrant collection name.
"""
import hashlib

from qdrant_client import QdrantClient, models

VECTOR_SIZE = 32


class QdrantSceneStore:
    def __init__(self, qdrant_url: str, collection: str):
        self._url = qdrant_url
        self.collection = collection

    def client(self) -> QdrantClient:
        return QdrantClient(url=self._url)

    def ensure_collection(self) -> None:
        c = self.client()
        names = [x.name for x in c.get_collections().collections]
        if self.collection not in names:
            c.create_collection(
                collection_name=self.collection,
                vectors_config=models.VectorParams(size=VECTOR_SIZE, distance=models.Distance.COSINE),
            )

    @staticmethod
    def vectorize(text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        values = [(b / 255.0) * 2.0 - 1.0 for b in digest]
        return (values * 2)[:VECTOR_SIZE]

    def upsert_scene(self, project_id: int, episode_id: int, text: str) -> None:
        try:
            self.ensure_collection()
            self.client().upsert(
                collection_name=self.collection,
                points=[
                    models.PointStruct(
                        id=episode_id,
                        vector=self.vectorize(text),
                        payload={
                            "project_id": project_id,
                            "episode_id": episode_id,
                            "text": text[:5000],
                        },
                    )
                ],
            )
        except Exception:
            # Qdrant is optional for the basic MVP.
            pass

    def search(self, project_id: int, query: str, limit: int = 5) -> list[dict]:
        try:
            self.ensure_collection()
            result = self.client().query_points(
                collection_name=self.collection,
                query=self.vectorize(query),
                query_filter=models.Filter(
                    must=[models.FieldCondition(key="project_id", match=models.MatchValue(value=project_id))]
                ),
                limit=limit,
            )
            return [p.payload for p in result.points]
        except Exception:
            return []

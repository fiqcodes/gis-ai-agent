"""Session-owned, expiring results and reports; small Redis REST chunks."""
import base64
import gzip
import json
import os
from storage_base import RedisStore, MemoryStore, StorageUnavailable, ConversationBusy

class ArtifactStore(RedisStore):
    def put(self, sid, artifact_id, data):
        encoded=base64.b64encode(gzip.compress(data)).decode('ascii')
        if len(data)>20_000_000 or len(encoded)>12_000_000:
            raise ValueError('Result is too large. Choose fewer indices or a smaller region.')
        chunks=[encoded[i:i+450_000] for i in range(0,len(encoded),450_000)]
        for i,chunk in enumerate(chunks):
            self.command('SET',self.key(f'file:{sid}:{artifact_id}:{i}'),chunk,'EX',self.ttl)
        self.command('SET',self.key(f'file:{sid}:{artifact_id}:count'),len(chunks),'EX',self.ttl)
    def get(self,sid,artifact_id):
        n=self.command('GET',self.key(f'file:{sid}:{artifact_id}:count'))
        if n is None: return None
        if not 1<=int(n)<=27: raise StorageUnavailable('Invalid stored result')
        chunks=[self.command('GET',self.key(f'file:{sid}:{artifact_id}:{i}')) for i in range(int(n))]
        if any(x is None for x in chunks): return None
        return gzip.decompress(base64.b64decode(''.join(chunks)))

class LocalStore(MemoryStore):
    def __init__(self):
        super().__init__(); self.artifacts={}
    def put(self,sid,artifact_id,data):
        import time
        self.artifacts[(sid,artifact_id)]=(data,time.time()+86400)
    def get(self,sid,artifact_id):
        import time
        item=self.artifacts.get((sid,artifact_id))
        return item[0] if item and item[1]>time.time() else None

def configured_store():
    url=os.getenv('UPSTASH_REDIS_REST_URL') or os.getenv('KV_REST_API_URL')
    token=os.getenv('UPSTASH_REDIS_REST_TOKEN') or os.getenv('KV_REST_API_TOKEN')
    if url and token:
        return ArtifactStore(url,token,prefix='orbiview:'+os.getenv('VERCEL_ENV','development'))
    return None if os.getenv('VERCEL') else LocalStore()

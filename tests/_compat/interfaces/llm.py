from typing import Protocol, runtime_checkable, List, Dict, Type, TypeVar, Optional
from pydantic import BaseModel
T = TypeVar('T', bound=BaseModel)

@runtime_checkable
class EmbeddingModel(Protocol):
    def embed(self, input: List) -> List: ...

@runtime_checkable
class LLM(Protocol):
    def generate(self, prompt: str, system_prompt: Optional[str] = None, max_tokens: int = 4000) -> str: ...

    def generate_structured(self, schema: Type[T], prompt: str, system_prompt: Optional[str] = None, max_tokens: int = 4000) -> T: ...

    def chat(self, message: List[Dict]) -> str: ...

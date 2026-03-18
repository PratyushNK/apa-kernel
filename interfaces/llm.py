from typing import Protocol, runtime_checkable
from typing import List, Dict, Type, TypeVar
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

@runtime_checkable
class EmbeddingModel(Protocol) : 

    def embed(self, input: List) -> List : 
        """Performs embedding on the input content and returns the vector"""
        ... 
        
@runtime_checkable
class LLM(Protocol) : 

    def generate(self, prompt: str, system_prompt: str | None = None, max_tokens: int = 4000) -> str : 
        """Generate response from prompt"""
        ... 

    def generate_structured(self, schema: Type[T], prompt: str, system_prompt: str | None = None, max_tokens: int = 4000) -> T : 
        """Generate response from prompt"""
        ... 

    def chat(self, message: List[Dict]) -> str : 
        """Chat completion with message history""" 
        ... 

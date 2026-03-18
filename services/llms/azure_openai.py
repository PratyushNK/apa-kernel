from typing import Literal, List, Dict, Any, cast, Type, TypeVar
from openai import AzureOpenAI
from openai.types.chat import ChatCompletion
from interfaces.llm import LLM , EmbeddingModel
from config import (
    AZURE_FOUNDRY_API_KEY, 
    AZURE_FOUNDRY_API_VERSION, 
    AZURE_FOUNDRY_ENDPOINT
)

from langchain_openai import AzureChatOpenAI
from pydantic import SecretStr
from langchain_core.messages import SystemMessage, HumanMessage
from langchain_core.output_parsers import StrOutputParser
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

llm_models = Literal["o4-mini", "gpt-4.1"] 
embedding_models = Literal["text-embedding-3-small"]

class AzureOpenAILLM() : 

    def __init__(self, deployment: llm_models) : 

        self.deployment = deployment
        # LangChain Azure wrapper (still Azure deployment)
        self._llm = AzureChatOpenAI(
            azure_endpoint=AZURE_FOUNDRY_ENDPOINT,
            azure_deployment=deployment,
            api_version=AZURE_FOUNDRY_API_VERSION,
            api_key=SecretStr(AZURE_FOUNDRY_API_KEY),
        )

    def generate(self, prompt: str, system_prompt: str | None = None, max_tokens: int = 4000) -> str : 
        
        messages = []
        
        if system_prompt:
            messages.append(SystemMessage(content=system_prompt))

        messages.append(HumanMessage(content=prompt))

        chain = self._llm | StrOutputParser()

        # max_tokens must be bound via config
        return chain.invoke(
            messages, 
            config={"configurable": {"max_tokens": max_tokens}}
        )
    
    def generate_structured(self, schema: Type[T], prompt: str, system_prompt: str | None = None, max_tokens: int = 4000) -> T:
        messages = []
        
        if system_prompt:
            messages.append(SystemMessage(content=system_prompt))

        messages.append(HumanMessage(content=prompt))

        structured_llm = (
            self._llm
            .with_structured_output(schema)
            .with_config(configurable={"max_tokens": max_tokens})
        )

        result = structured_llm.invoke(messages)

        return cast(T, result)


    def chat(self, message: List[Dict[str, Any]]) -> str : 

        chain = self._llm | StrOutputParser()

        return chain.invoke(
            message,
            config={"configurable": {"max_tokens": 40000}}
        )

class AzureOpenAIEmbeddingModel() : 

    def __init__(self, deployment : embedding_models) : 

        self.deployment = deployment
        self.client = AzureOpenAI(
            api_version=AZURE_FOUNDRY_API_VERSION,
            azure_endpoint=AZURE_FOUNDRY_ENDPOINT,
            api_key= AZURE_FOUNDRY_API_KEY
        )

    def embed(self, input: List[str], verbose: bool = False) -> List[List[float]]:
        
        # Filter out empty strings and None values
        valid_input = [text for text in input if text and text.strip()]
        
        if not valid_input:
            # Return empty list if no valid input
            return []
        
        response = self.client.embeddings.create(
            input=valid_input,
            model=self.deployment
        )

        if verbose : 
            for item in response.data:
                length = len(item.embedding)
                print(
                    f"data[{item.index}]: length={length}, "
                    f"[{item.embedding[0]}, {item.embedding[1]}, "
                    f"..., {item.embedding[length-2]}, {item.embedding[length-1]}]"
                )
            print(response.usage)

        # Extract the actual embedding vectors (list of floats) from the Embedding objects
        return [item.embedding for item in response.data]


class Colour(BaseModel):
    colour: str

if __name__ == "__main__" : 

    def test_azure_openai_llm(model: llm_models, prompt: str):

        llm: LLM = AzureOpenAILLM(model)
        colour_response = llm.generate_structured(Colour, prompt)
        response = llm.generate(prompt)
        print(colour_response.colour)
        print(response)

    def test(em_mod: EmbeddingModel, llm: LLM) : 

        # to test embedding model
        input = ["first phrase","second phrase","third phrase"] 
        res = em_mod.embed(input)  
        print(type(res))

        # to test LLM model 
        res = llm.generate("what is the colour of the sky?") 
        print(res)
    
    #em_mod = AzureOpenAIEmbeddingModel(deployment="text-embedding-3-small") 
    #llm  = AzureOpenAILLM("gpt-4.1") 
    #print(isinstance(llm, LLM))
    #test(em_mod, llm) 
    test_azure_openai_llm("o4-mini", "what is the colour of the sky? answer in one word.")
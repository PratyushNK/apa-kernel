from typing import List, Dict, Any, cast, Type, TypeVar
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import SystemMessage, HumanMessage
from interfaces.llm import LLM
from config import load_env 
from langchain_core.output_parsers import StrOutputParser
from pydantic import BaseModel
import json

T = TypeVar("T", bound=BaseModel)

class GeminiLLM() : 

    def __init__(self, model: str = "gemini-2.5-flash", temp: float = 0.0, max_tokens: int = 128, max_retries: int = 2) : 

        load_env() 
        self.model = ChatGoogleGenerativeAI(
            model = model,
            temperature = temp,     # IMPORTANT for normalization to produce results deterministically. 
            max_tokens = max_tokens,
            max_retries = max_retries,
        )

    def generate(self, prompt: str, system_prompt: str | None = None, max_tokens: int = 4000) -> str:
        messages = []
        
        if system_prompt:
            messages.append(SystemMessage(content=system_prompt))

        messages.append(HumanMessage(content=prompt))

        chain = self.model | StrOutputParser()

        # max_tokens must be bound via config
        result = chain.invoke(
            messages,
            config={"configurable": {"max_tokens": max_tokens}},
        )

        # Token usage extraction fallback
        input_tokens = None
        output_tokens = None
        try:
            llm_out = getattr(result, "llm_output", None)
            if llm_out and isinstance(llm_out, dict):
                usage = llm_out.get("token_usage") or llm_out.get("usage")
                if isinstance(usage, dict):
                    input_tokens = usage.get("prompt_tokens") or usage.get("input_tokens")
                    output_tokens = usage.get("completion_tokens") or usage.get("output_tokens") or usage.get("total_tokens")
        except Exception:
            pass

        if input_tokens is None or output_tokens is None:
            try:
                input_tokens = len(prompt.split())
                output_tokens = len(str(result).split())
            except Exception:
                input_tokens = "unknown"
                output_tokens = "unknown"

        print(f"\nInput tokens: {input_tokens}\nOutput tokens: {output_tokens}\n")

        return result
    
    
    def generate_structured(self, schema: Type[T], prompt: str, system_prompt: str | None = None, max_tokens: int = 4000) -> T:

        messages = []
        
        if system_prompt:
            messages.append(SystemMessage(content=system_prompt))

        messages.append(HumanMessage(content=prompt))

        structured_llm = (
            self.model
            .with_structured_output(schema, method="function_calling")
            .with_config(configurable={"max_tokens": max_tokens})
        )

        result = structured_llm.invoke(messages)
        print(f"[gemini_debug] type={type(result)} value={result}")

        input_tokens = None
        output_tokens = None
        try:
            llm_out = getattr(result, "llm_output", None)
            if llm_out and isinstance(llm_out, dict):
                usage = llm_out.get("token_usage") or llm_out.get("usage")
                if isinstance(usage, dict):
                    input_tokens = usage.get("prompt_tokens") or usage.get("input_tokens")
                    output_tokens = usage.get("completion_tokens") or usage.get("output_tokens") or usage.get("total_tokens")
            raw = getattr(result, "raw_response", None)
            if (input_tokens is None or output_tokens is None) and isinstance(raw, dict):
                usage = raw.get("usage")
                if isinstance(usage, dict):
                    input_tokens = input_tokens or usage.get("prompt_tokens")
                    output_tokens = output_tokens or usage.get("completion_tokens") or usage.get("total_tokens")
        except Exception:
            pass

        if input_tokens is None or output_tokens is None:
            try:
                input_tokens = len(prompt.split())
                output_tokens = len(str(result).split())
            except Exception:
                input_tokens = "unknown"
                output_tokens = "unknown"

        print(f"\nInput tokens: {input_tokens}\nOutput tokens: {output_tokens}\n")

        return cast(T, result)
    

    # def generate_structured(self, schema: Type[T], prompt: str, system_prompt: str | None = None, max_tokens: int = 4000) -> T:
    #     # Gemini doesn't reliably support with_structured_output
    #     # Use plain generate + manual JSON parse instead
    
    #     json_prompt = prompt + "\n\nRespond with valid JSON only. No markdown, no backticks, no explanation outside the JSON object."
    
    #     raw = self.generate(json_prompt, system_prompt=system_prompt, max_tokens=max_tokens)
    
    #     # Strip markdown code fences if present
    #     clean = raw.strip()
    #     if clean.startswith("```"):
    #         clean = clean.split("```")[1]
    #         if clean.startswith("json"):
    #             clean = clean[4:]
    #     clean = clean.strip()
    
    #     try:
    #         data = json.loads(clean)
    #         return schema(**data)
    #     except Exception as e:
    #         print(f"[gemini_debug] parse failed — raw='{raw}' error={e}")
    #         return schema()


    def chat(self, message: List[Dict[str, Any]]) -> str:
        """
        Accepts a list of message dictionaries with 'role' and 'content' keys.
        Converts them to LangChain message types and invokes the model.
        """
        langchain_messages = []
        
        for msg in message:
            role = msg.get('role', '')
            content = msg.get('content', '')
            
            if role == 'system':
                langchain_messages.append(SystemMessage(content=content))
            elif role in ['user', 'human']:
                langchain_messages.append(HumanMessage(content=content))
            else:
                # Default to HumanMessage for unknown roles
                langchain_messages.append(HumanMessage(content=content))
        
        ai_msg = self.model.invoke(langchain_messages)
        
        content = getattr(ai_msg, 'content', None)
        return content or ""


class Colour(BaseModel):
    colour: str

if __name__ == "__main__" : 

    def test_gemini_llm(prompt: str):

        llm: LLM = GeminiLLM()
        colour_response = llm.generate_structured(Colour, prompt)
        response = llm.generate(prompt)
        print(colour_response.colour)
        print(response)

    #gemini : LLM = GeminiLLM() 
    #prompt = "I love programming." 
    #system = "You are a helpful assistant that translates English to French. Translate the user sentence."
    #content = gemini.generate(system, prompt) 
    #print(content)

    test_gemini_llm("what is the colour of the sky? answer in one word.")
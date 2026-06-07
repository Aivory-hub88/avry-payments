from abc import ABC, abstractmethod
from app.llm import OllamaClient

class BaseAgent(ABC):
    def __init__(self, llm_client: OllamaClient):
        self.llm = llm_client
    
    @abstractmethod
    async def process(self, input_data: dict) -> dict:
        """Process input and return output"""
        pass

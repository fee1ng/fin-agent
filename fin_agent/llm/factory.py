from fin_agent.config import Config
from fin_agent.llm.deepseek_client import DeepSeekClient
from fin_agent.llm.openai_client import OpenAICompatibleClient

class LLMFactory:
    @staticmethod
    def create_llm():
        provider = Config.LLM_PROVIDER
        
        if provider == "deepseek":
            return DeepSeekClient()
        elif provider == "openrouter":
            return OpenAICompatibleClient(
                api_key=Config.OPENAI_API_KEY,
                base_url=Config.OPENAI_BASE_URL,
                model=Config.OPENAI_MODEL,
                default_headers={
                    "HTTP-Referer": "https://github.com/fin-agent/fin-agent",
                    "X-Title": "Fin-Agent CLI"
                }
            )
        elif provider == "openai" or provider == "local":
            # Generic OpenAI compatible
            return OpenAICompatibleClient(
                api_key=Config.OPENAI_API_KEY,
                base_url=Config.OPENAI_BASE_URL,
                model=Config.OPENAI_MODEL
            )
        else:
            raise ValueError(f"Unsupported LLM provider: {provider}")


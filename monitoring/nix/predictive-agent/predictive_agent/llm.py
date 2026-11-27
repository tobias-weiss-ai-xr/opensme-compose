"""Multi-backend LLM analyzer (Ollama, SAIA, TUD, OpenAI)."""

import json
from enum import Enum


class LLMBackend(Enum):
    """LLM backend enum."""
    OLLAMA = "ollama"
    SAIA = "saia"
    TUD = "tud"
    OPENAI = "openai"


class LLMAnalyzer:
    """Multi-backend LLM analyzer for Kubernetes health analysis."""

    def __init__(self, backend, url, model, api_key=None, timeout=180):
        """Initialize LLM analyzer.
        
        Args:
            backend: LLMBackend enum value
            url: API endpoint URL
            model: Model name
            api_key: Optional API key (required for SAIA, TUD, OpenAI)
            timeout: API request timeout in seconds
        """
        self.backend = backend
        self.url = url
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    def build_prompt(self, issue, context, prediction=None):
        """Build LLM prompt with issue, context, and prediction data.
        
        Args:
            issue: Issue description
            context: Context information
            prediction: Optional prediction data dict with keys:
                - risk_score: Risk score (0-1)
                - ttf_minutes: Time to failure in minutes
                - confidence: Confidence score (0-1)
                - markov_state: Markov state string
                - memory_trend: Memory trend value
        
        Returns:
            Formatted prompt string
        """
        prompt_parts = []
        prompt_parts.append(f"Issue: {issue}")
        prompt_parts.append(f"Context: {context}")
        
        if prediction:
            prompt_parts.append("\nPrediction data:")
            if "risk_score" in prediction:
                prompt_parts.append(f"  Risk score: {prediction['risk_score']}")
            if "ttf_minutes" in prediction:
                prompt_parts.append(f"  Time to failure: {prediction['ttf_minutes']} minutes")
            if "confidence" in prediction:
                prompt_parts.append(f"  Confidence: {prediction['confidence']}")
            if "markov_state" in prediction:
                prompt_parts.append(f"  Markov state: {prediction['markov_state']}")
            if "memory_trend" in prediction:
                prompt_parts.append(f"  Memory trend: {prediction['memory_trend']}")
        
        prompt_parts.append("\nReturn exactly one JSON object with these keys:") 
        prompt_parts.append('{"analysis": "brief root-cause analysis", "severity": "high|medium|low", "action": "recommended action", "command": "docker/kubectl command if applicable, else empty string"}')
        prompt_parts.append("Do not wrap the JSON in markdown fences and do not add prose outside the JSON object.")
        
        return "\n".join(prompt_parts)

    def analyze(self, issue, context, prediction=None):
        """Perform analysis using the configured LLM backend.
        
        Args:
            issue: Issue description
            context: Context information
            prediction: Optional prediction data dict
            
        Returns:
            Parsed analysis result dict
        """
        prompt = self.build_prompt(issue, context, prediction)
        
        try:
            import urllib.request
            import urllib.error
            
            if self.backend == LLMBackend.OLLAMA:
                endpoint = f"{self.url.rstrip('/')}/api/chat"
                payload = {
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False
                }
                headers = {"Content-Type": "application/json"}
            else:
                # OpenAI compatible (SAIA, TUD, OPENAI)
                endpoint = f"{self.url.rstrip('/')}/chat/completions"
                payload = {
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}]
                }
                headers = {"Content-Type": "application/json"}
                if self.api_key:
                    headers["Authorization"] = f"Bearer {self.api_key}"

            data = json.dumps(payload).encode("utf-8")
            request = urllib.request.Request(endpoint, data=data, headers=headers)
            
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
                res_json = json.loads(body)
                
                if self.backend == LLMBackend.OLLAMA:
                    # Ollama returns content in 'message' -> 'content' or similar depending on version, 
                    # but tests expect 'response' field
                    content = res_json.get("response", res_json.get("message", {}).get("content", ""))
                else:
                    # OpenAI compatible: choices[0].message.content
                    choices = res_json.get("choices", [])
                    content = choices[0].get("message", {}).get("content", "") if choices else ""
                
                return self.parse_response(content)

        except Exception as e:
            return {
                "analysis": f"LLM API error: {str(e)}",
                "severity": "medium",
                "action": "Check LLM connectivity",
                "command": ""
            }

    def parse_response(self, response):
        """Parse LLM JSON response with fallback for invalid JSON.
        
        Models may return JSON wrapped in markdown fences or prefixed with
        prose; extract the first JSON object rather than requiring the whole
        response to be pure JSON.
        """
        try:
            parsed = self._extract_json(response)
            if not isinstance(parsed, dict):
                return self._fallback_response()
            
            if "analysis" not in parsed:
                parsed["analysis"] = "Unknown issue"
            if "severity" not in parsed:
                parsed["severity"] = "medium"
            if "action" not in parsed:
                parsed["action"] = "Monitor"
            if "command" not in parsed:
                parsed["command"] = ""
            
            return parsed
        except (json.JSONDecodeError, TypeError, AttributeError):
            return self._fallback_response()

    @staticmethod
    def _extract_json(response):
        """Extract the first JSON object from a possibly fenced/prose response."""
        if not isinstance(response, str):
            return None
        text = response.strip()
        start = text.find("{")
        if start < 0:
            return None
        try:
            return json.JSONDecoder().raw_decode(text[start:])[0]
        except (json.JSONDecodeError, TypeError, ValueError):
            return None

    def _fallback_response(self):
        """Generate fallback response for invalid JSON."""
        return {
            "analysis": "Unknown issue - invalid LLM response",
            "severity": "medium",
            "action": "Monitor",
            "command": ""
        }

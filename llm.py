"""Groq transport for the existing parser, GIS insights and research sections."""
import os
import requests
from config import GROQ_URL, GROQ_MODEL

class LLMUnavailable(RuntimeError):
    pass

class Completion:
    def __init__(self, content):
        self.content = content
    def json(self):
        # Preserve the internal completion contract used by the analysis functions.
        return {'message': {'content': self.content}}

def llm_post(url=None, *, json, timeout=30):
    key = os.getenv('GROQ_API_KEY')
    if not key:
        raise LLMUnavailable('Groq is not configured by the owner.')
    payload = {'model': GROQ_MODEL, 'messages': json['messages'],
               'temperature': 0, 'max_completion_tokens': 3072, 'stream': False}
    if GROQ_MODEL.startswith('openai/gpt-oss'):
        payload['reasoning_effort'] = 'low'
        payload['reasoning_format'] = 'hidden'
    if json.get('format') == 'json':
        payload['response_format'] = {'type': 'json_object'}
    try:
        response = requests.post(GROQ_URL, json=payload,
            headers={'Authorization': f'Bearer {key}'}, timeout=(5, min(timeout, 35)),
            allow_redirects=False)
        response.raise_for_status()
        content = response.json()['choices'][0]['message']['content']
        if not isinstance(content, str) or not content.strip():
            raise ValueError('Empty model response')
        return Completion(content)
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as exc:
        raise LLMUnavailable('The AI provider is unavailable or its quota was reached. Please try again later.') from exc

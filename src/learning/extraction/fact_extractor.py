"""Semantic fact candidate extractor."""
from src.learning.extraction.base import BaseExtractor
from src.learning.extraction.contracts import CandidateTarget


class FactExtractor(BaseExtractor):
    target_type = CandidateTarget.SEMANTIC_FACT
    extractor_version = "semantic-fact-extractor-v2"
    prompt_name = "semantic-fact-prompt-v1"
    prompt_file = "fact-v1.md"

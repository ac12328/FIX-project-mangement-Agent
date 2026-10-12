"""Operational procedure candidate extractor."""
from src.learning.extraction.base import BaseExtractor
from src.learning.extraction.contracts import CandidateTarget


class ProcedureExtractor(BaseExtractor):
    target_type = CandidateTarget.PROCEDURE
    extractor_version = "procedure-extractor-v2"
    prompt_name = "procedure-prompt-v1"
    prompt_file = "procedure-v1.md"

"""Communication preference candidate extractor."""
from src.learning.extraction.base import BaseExtractor
from src.learning.extraction.contracts import CandidateTarget


class PreferenceExtractor(BaseExtractor):
    target_type = CandidateTarget.PREFERENCE
    extractor_version = "preference-extractor-v2"
    prompt_name = "preference-prompt-v1"
    prompt_file = "preference-v1.md"

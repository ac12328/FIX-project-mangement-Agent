"""Wiki-publication candidate extractor; this module never publishes."""
from src.learning.extraction.base import BaseExtractor
from src.learning.extraction.contracts import CandidateTarget


class WikiPublicationExtractor(BaseExtractor):
    target_type = CandidateTarget.WIKI_PUBLICATION
    extractor_version = "wiki-publication-extractor-v2"
    prompt_name = "wiki-publication-prompt-v1"
    prompt_file = "wiki-v1.md"

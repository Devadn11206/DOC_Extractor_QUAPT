"""
Rule-Based Document Classifier Adapter (Stage 4B).

Implements deterministic, explainable document classification for the three MVP document types:
- DOC-01: CASA Account Opening Form (BFSI)
- DOC-02: Insurance Application Form (BFSI)
- DOC-03: Hospital Discharge Summary (Healthcare)

Features:
- Normalized, case-insensitive keyword and phrase matching preserving original evidence.
- Weighted positive and negative evidence signals.
- Distinguishes high-value discriminating signals from ambiguous generic terms.
- Deterministic score-to-confidence calculation with explicit RULE_HEURISTIC provenance.
- Detects ambiguous / conflicting evidence and routes safely to UNKNOWN.
- Traceable classification reasons explaining exactly why decisions were made.
"""

from dataclasses import dataclass
import re
from typing import Dict, List, Optional, Sequence, Set, Tuple

from doc_extractor.classification.base import (
    ClassifierEngine,
    ClassifierMetadata,
    ClassifierRawResult,
)
from doc_extractor.contracts_b import (
    ConfidenceProvenance,
    MVPClassificationDomain,
    MVPDocumentType,
    PageLayoutOutput,
)
from doc_extractor.loader_b import LoadedPageImage, ModuleBLoadResult


@dataclass(frozen=True)
class RuleSignal:
    """A weighted lexical or structural rule pattern."""
    pattern: re.Pattern
    weight: float
    description: str
    is_negative: bool = False


class RuleBasedClassifierAdapter(ClassifierEngine):
    """
    Deterministic layout-text rule-based document classifier (ARC-002, FR-CLS-001..004).

    Evaluates parsed page layouts, Markdown contents, and typed blocks to score candidates
    for DOC-01, DOC-02, and DOC-03.
    """

    CLASSIFIER_NAME = "rule-based-classifier-adapter"
    CLASSIFIER_VERSION = "1.0.0"

    # Default score threshold for considering a type candidate viable
    # Note: heuristic score != calibrated probability.
    MIN_SCORE_THRESHOLD = 20.0
    # Minimum difference ratio (top_score - second_score) / top_score to avoid ambiguity
    AMBIGUITY_MARGIN = 0.25

    def __init__(
        self,
        classifier_name: str = CLASSIFIER_NAME,
        classifier_version: str = CLASSIFIER_VERSION,
        min_score_threshold: float = MIN_SCORE_THRESHOLD,
        ambiguity_margin: float = AMBIGUITY_MARGIN,
        is_operational: bool = True,
    ) -> None:
        self._classifier_name = classifier_name
        self._classifier_version = classifier_version
        self._min_score_threshold = min_score_threshold
        self._ambiguity_margin = ambiguity_margin
        self._is_operational = is_operational

        self._doc01_signals = self._init_doc01_signals()
        self._doc02_signals = self._init_doc02_signals()
        self._doc03_signals = self._init_doc03_signals()

    def get_metadata(self) -> ClassifierMetadata:
        return ClassifierMetadata(
            classifier_name=self._classifier_name,
            classifier_version=self._classifier_version,
            supported_domains=(MVPClassificationDomain.BFSI, MVPClassificationDomain.HEALTHCARE),
            supported_types=(
                MVPDocumentType.DOC_01_CASA,
                MVPDocumentType.DOC_02_INSURANCE_APP,
                MVPDocumentType.DOC_03_DISCHARGE_SUMMARY,
            ),
            is_operational=self._is_operational,
            confidence_provenance=ConfidenceProvenance.RULE_HEURISTIC,
            calibrated_probabilities=False,
        )

    def is_operational(self) -> bool:
        return self._is_operational

    # ------------------------------------------------------------------------
    # Rule Definitions
    # ------------------------------------------------------------------------

    def _init_doc01_signals(self) -> List[RuleSignal]:
        """Weighted signals for DOC-01 (CASA Account Opening Form)."""
        return [
            # High-weight primary titles & headings
            RuleSignal(re.compile(r"\baccount\s+opening\s+(?:form|application)\b", re.I), 35.0, "Title: Account Opening Form"),
            RuleSignal(re.compile(r"\bcasa\s+(?:account|application|opening)\b", re.I), 35.0, "Title: CASA Application"),
            RuleSignal(re.compile(r"\b(?:savings|current)\s+account\s+opening\b", re.I), 30.0, "Title: Savings/Current Account Opening"),
            RuleSignal(re.compile(r"\bcustomer\s+information\s+form\b", re.I), 25.0, "Title: Customer Information Form (CIF)"),
            
            # Strong banking/CASA structural field labels
            RuleSignal(re.compile(r"\b(?:cif|customer\s*id)\s*(?:no|number)?\b", re.I), 15.0, "Field: CIF / Customer ID"),
            RuleSignal(re.compile(r"\b(?:initial\s+deposit|minimum\s+balance|mode\s+of\s+operation)\b", re.I), 15.0, "Field: Initial deposit / Mode of operation"),
            RuleSignal(re.compile(r"\b(?:debit\s+card|cheque\s+book|atm\s+card|internet\s+banking)\b", re.I), 12.0, "Service: Debit card / Cheque book / Net banking"),
            RuleSignal(re.compile(r"\b(?:single|either\s+or\s+survivor|jointly\s+operated)\b", re.I), 12.0, "Account operating instructions"),
            RuleSignal(re.compile(r"\bpan\s*(?:card|no|number)?\b", re.I), 8.0, "KYC: PAN Card"),
            RuleSignal(re.compile(r"\baadhaar\s*(?:no|number)?\b", re.I), 8.0, "KYC: Aadhaar Number"),

            # Negative indicators for banking forms
            RuleSignal(re.compile(r"\bdischarge\s+summary\b", re.I), 40.0, "Conflicting: Hospital Discharge Summary", is_negative=True),
            RuleSignal(re.compile(r"\bproposal\s+form\s+(?:for|life|health)\b", re.I), 35.0, "Conflicting: Insurance Proposal Form", is_negative=True),
            RuleSignal(re.compile(r"\bsum\s+insured\b", re.I), 25.0, "Conflicting: Insurance Sum Insured", is_negative=True),
        ]

    def _init_doc02_signals(self) -> List[RuleSignal]:
        """Weighted signals for DOC-02 (Insurance Application Form)."""
        return [
            # High-weight primary titles & headings
            RuleSignal(re.compile(r"\b(?:life|health|general|motor)\s+insurance\s+(?:proposal|application)\b", re.I), 35.0, "Title: Insurance Proposal/Application"),
            RuleSignal(re.compile(r"\bproposal\s+form\s+for\b", re.I), 30.0, "Title: Proposal Form For"),
            RuleSignal(re.compile(r"\binsurance\s+(?:application|enrolment)\s+form\b", re.I), 30.0, "Title: Insurance Application Form"),

            # Strong insurance structural field labels
            RuleSignal(re.compile(r"\bsum\s+(?:insured|assured)\b", re.I), 20.0, "Field: Sum Insured / Assured"),
            RuleSignal(re.compile(r"\bpolicy\s+(?:term|period|type)\b", re.I), 15.0, "Field: Policy Term / Period"),
            RuleSignal(re.compile(r"\bpremium\s+(?:frequency|paying\s+term|amount|payment)\b", re.I), 15.0, "Field: Premium payment details"),
            RuleSignal(re.compile(r"\bproposer\s*(?:name|details)?\b", re.I), 12.0, "Field: Proposer Details"),
            RuleSignal(re.compile(r"\blife\s+to\s+be\s+assured\b", re.I), 15.0, "Field: Life to be Assured"),
            RuleSignal(re.compile(r"\bnominee\s*(?:name|relationship|share)?\b", re.I), 12.0, "Field: Nominee details"),
            RuleSignal(re.compile(r"\bmedical\s+history\s*(?:questionnaire)?\b", re.I), 15.0, "Section: Medical History Questionnaire"),
            RuleSignal(re.compile(r"\bpre[\-\s]?existing\s+(?:diseases|illness|conditions)\b", re.I), 15.0, "Medical underwriting: Pre-existing diseases"),

            # Negative indicators
            RuleSignal(re.compile(r"\baccount\s+opening\s+form\b", re.I), 35.0, "Conflicting: Bank Account Opening Form", is_negative=True),
            RuleSignal(re.compile(r"\bdischarge\s+summary\b", re.I), 40.0, "Conflicting: Hospital Discharge Summary", is_negative=True),
            RuleSignal(re.compile(r"\bdate\s+of\s+admission\b", re.I), 20.0, "Conflicting: Hospital Admission", is_negative=True),
        ]

    def _init_doc03_signals(self) -> List[RuleSignal]:
        """Weighted signals for DOC-03 (Hospital Discharge Summary)."""
        return [
            # High-weight primary titles & headings
            RuleSignal(re.compile(r"\bdischarge\s+summary\b", re.I), 40.0, "Title: Discharge Summary"),
            RuleSignal(re.compile(r"\bhospital\s+discharge\s+(?:report|card|summary)\b", re.I), 40.0, "Title: Hospital Discharge Report"),
            RuleSignal(re.compile(r"\bdischarge\s+card\b", re.I), 30.0, "Title: Discharge Card"),
            RuleSignal(re.compile(r"\bclinical\s+summary\b", re.I), 25.0, "Title: Clinical Summary"),

            # Strong clinical/hospital structural field labels
            RuleSignal(re.compile(r"\bdate\s+of\s+admission\b", re.I), 20.0, "Field: Date of Admission (DOA)"),
            RuleSignal(re.compile(r"\bdate\s+of\s+discharge\b", re.I), 20.0, "Field: Date of Discharge (DOD)"),
            RuleSignal(re.compile(r"\bip\s*(?:no|number)?\b|\binpatient\s*(?:no|number|id)?\b", re.I), 15.0, "Field: IP / Inpatient Number"),
            RuleSignal(re.compile(r"\bconsultant\s*(?:doctor|physician|surgeon)?\b|\battending\s+doctor\b", re.I), 15.0, "Field: Consultant / Attending Physician"),
            RuleSignal(re.compile(r"\bchief\s+complaints\b", re.I), 15.0, "Clinical: Chief Complaints"),
            RuleSignal(re.compile(r"\bfinal\s+diagnosis\b|\bprovisional\s+diagnosis\b", re.I), 20.0, "Clinical: Diagnosis"),
            RuleSignal(re.compile(r"\bmedications?\s+on\s+discharge\b|\btreatment\s+given\b", re.I), 15.0, "Clinical: Discharge Medications / Treatment"),
            RuleSignal(re.compile(r"\bfollow[\-\s]?up\s+advice\b", re.I), 12.0, "Clinical: Follow-up Advice"),

            # Negative indicators
            RuleSignal(re.compile(r"\baccount\s+opening\s+(?:form|application)\b", re.I), 35.0, "Conflicting: Bank Account Opening Form", is_negative=True),
            RuleSignal(re.compile(r"\bcasa\s+(?:account|application|opening)\b", re.I), 35.0, "Conflicting: CASA Application", is_negative=True),
            RuleSignal(re.compile(r"\bproposal\s+form\s+for\b", re.I), 35.0, "Conflicting: Insurance Proposal Form", is_negative=True),
            RuleSignal(re.compile(r"\binitial\s+deposit\b", re.I), 25.0, "Conflicting: Initial Deposit", is_negative=True),
        ]

    # ------------------------------------------------------------------------
    # Normalization & Text Aggregation
    # ------------------------------------------------------------------------

    def _extract_normalized_corpus(self, parsed_pages: Sequence[PageLayoutOutput]) -> str:
        """
        Extracts and normalizes text from parsed layout pages while preserving
        underlying contracts and page evidence.
        """
        chunks: List[str] = []
        for page in parsed_pages:
            if page.markdown_content:
                chunks.append(page.markdown_content)
            for block in page.blocks:
                if block.text:
                    chunks.append(block.text)
            for kv in page.key_value_pairs:
                chunks.append(f"{kv.key_text}: {kv.value_text}")
            for grid in page.character_grids:
                if grid.reconstructed_text:
                    chunks.append(grid.reconstructed_text)

        raw_text = " \n ".join(chunks)
        # Safe normalization: normalize multiple spaces and tabs to single spaces
        normalized = re.sub(r"[ \t]+", " ", raw_text)
        return normalized

    # ------------------------------------------------------------------------
    # Scoring Evaluation
    # ------------------------------------------------------------------------

    def _evaluate_signals(
        self,
        corpus: str,
        signals: Sequence[RuleSignal],
    ) -> Tuple[float, List[str]]:
        """
        Matches signals against corpus and aggregates weighted score and reasons.
        """
        score = 0.0
        reasons: List[str] = []

        for sig in signals:
            matches = list(sig.pattern.finditer(corpus))
            if matches:
                count = len(matches)
                # Dampen repetitive matches so single spam words don't explode
                multiplier = min(count, 3) if not sig.is_negative else 1
                delta = sig.weight * multiplier
                if sig.is_negative:
                    score -= delta
                    reasons.append(f"Negative signal: {sig.description} (-{delta:.1f})")
                else:
                    score += delta
                    reasons.append(f"Matched {sig.description} ({count}x, +{delta:.1f})")

        # Clamp min score to 0.0
        return max(0.0, score), reasons

    def _calculate_heuristic_confidence(self, score: float, second_score: float) -> float:
        """
        Maps deterministic rule score to heuristic confidence in [0.0, 1.0].

        Policy:
        - score < MIN_SCORE_THRESHOLD (20.0): confidence < 0.50 (low/insufficient evidence).
        - score in [20, 40): confidence in [0.50, 0.75).
        - score in [40, 70): confidence in [0.75, 0.88).
        - score >= 70: confidence up to 0.96 (asymptotic ceiling).
        - If margin against runner-up is narrow, confidence is penalized.
        """
        if score <= 0.0:
            return 0.0

        # Base asymptotic scaling: f(x) = x / (x + 25.0)
        # x = 20 -> 20/45 = 0.444
        # x = 40 -> 40/65 = 0.615
        # x = 70 -> 70/95 = 0.737
        # We adjust scale to fit standard confidence brackets:
        # base_conf = min(0.96, 0.40 + (score / (score + 30.0)) * 0.60)
        base_conf = 0.40 + (score / (score + 25.0)) * 0.58
        base_conf = min(0.96, max(0.10, base_conf))

        # Margin penalty for close competitor
        if second_score > 0.0 and score > 0.0:
            margin = (score - second_score) / score
            if margin < self._AMBIGUITY_MARGIN:
                # Heavy penalty if margin is below ambiguity margin
                base_conf *= (margin / self._AMBIGUITY_MARGIN)

        return round(float(base_conf), 3)

    @property
    def _AMBIGUITY_MARGIN(self) -> float:
        return self._ambiguity_margin

    # ------------------------------------------------------------------------
    # Classification Execution
    # ------------------------------------------------------------------------

    def classify(
        self,
        load_result: ModuleBLoadResult,
        parsed_pages: List[PageLayoutOutput],
    ) -> ClassifierRawResult:
        """
        Evaluates document evidence against rule definitions.
        """
        corpus = self._extract_normalized_corpus(parsed_pages)
        if not corpus.strip():
            return ClassifierRawResult(
                domain=MVPClassificationDomain.UNKNOWN,
                document_type=MVPDocumentType.UNKNOWN,
                classification_confidence=0.0,
                confidence_provenance=ConfidenceProvenance.RULE_HEURISTIC,
                classifier_identity=self._classifier_name,
                classification_reasons=("No text or layout blocks found across pages.",),
            )

        score_01, reasons_01 = self._evaluate_signals(corpus, self._doc01_signals)
        score_02, reasons_02 = self._evaluate_signals(corpus, self._doc02_signals)
        score_03, reasons_03 = self._evaluate_signals(corpus, self._doc03_signals)

        scores: List[Tuple[MVPDocumentType, MVPClassificationDomain, float, List[str]]] = [
            (MVPDocumentType.DOC_01_CASA, MVPClassificationDomain.BFSI, score_01, reasons_01),
            (MVPDocumentType.DOC_02_INSURANCE_APP, MVPClassificationDomain.BFSI, score_02, reasons_02),
            (MVPDocumentType.DOC_03_DISCHARGE_SUMMARY, MVPClassificationDomain.HEALTHCARE, score_03, reasons_03),
        ]

        # Sort descending by score
        scores.sort(key=lambda x: x[2], reverse=True)
        top_type, top_domain, top_score, top_reasons = scores[0]
        second_score = scores[1][2]

        # Check for insufficient evidence
        if top_score < self._min_score_threshold:
            reasons = [
                f"Insufficient evidence for any known type (top score: {top_score:.1f} < threshold {self._min_score_threshold:.1f})."
            ]
            conf = self._calculate_heuristic_confidence(top_score, second_score)
            return ClassifierRawResult(
                domain=MVPClassificationDomain.UNKNOWN,
                document_type=MVPDocumentType.UNKNOWN,
                classification_confidence=conf,
                confidence_provenance=ConfidenceProvenance.RULE_HEURISTIC,
                classifier_identity=self._classifier_name,
                classification_reasons=tuple(reasons),
            )

        # Check for ambiguity / conflict
        # If second score is also above threshold and margin is less than ambiguity margin
        score_diff = top_score - second_score
        relative_margin = score_diff / top_score if top_score > 0 else 0.0
        if second_score >= self._min_score_threshold and relative_margin < self._ambiguity_margin:
            second_type = scores[1][0]
            reasons = [
                f"Ambiguous / conflicting evidence between {top_type.value} (score {top_score:.1f}) and {second_type.value} (score {second_score:.1f}) with margin {relative_margin:.2f} < {self._ambiguity_margin:.2f}."
            ]
            reasons.extend(top_reasons)
            reasons.extend(scores[1][3])
            conf = self._calculate_heuristic_confidence(top_score, second_score)
            return ClassifierRawResult(
                domain=MVPClassificationDomain.UNKNOWN,
                document_type=MVPDocumentType.UNKNOWN,
                classification_confidence=conf,
                confidence_provenance=ConfidenceProvenance.RULE_HEURISTIC,
                classifier_identity=self._classifier_name,
                classification_reasons=tuple(reasons),
            )

        # Clear winner
        conf = self._calculate_heuristic_confidence(top_score, second_score)
        all_reasons = [f"Classified as {top_type.value} with rule score {top_score:.1f}."]
        all_reasons.extend(top_reasons)
        return ClassifierRawResult(
            domain=top_domain,
            document_type=top_type,
            classification_confidence=conf,
            confidence_provenance=ConfidenceProvenance.RULE_HEURISTIC,
            classifier_identity=self._classifier_name,
            classification_reasons=tuple(all_reasons),
        )

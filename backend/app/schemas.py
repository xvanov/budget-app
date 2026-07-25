from pydantic import BaseModel
from typing import Optional, List

class TransactionCreate(BaseModel):
    external_id: Optional[str] = None
    source_platform: str
    source_file: str
    account_id: str
    posted_date: str
    transaction_date: Optional[str] = None
    amount_cents: int
    currency: str = "USD"
    merchant: str = ""
    description: str = ""

class TransactionOut(BaseModel):
    id: int
    external_id: Optional[str]
    source_platform: str
    source_file: str
    account_id: str
    posted_date: str
    transaction_date: Optional[str]
    amount_cents: int
    currency: str
    merchant: str
    description: str
    category_path: Optional[str]
    entity_type: Optional[str]
    asset_class: Optional[str]
    business_context: Optional[str]
    linked_transaction_id: Optional[int]
    confidence_score: Optional[float]
    categorization_reason: Optional[str]
    applied_rule_id: Optional[int]
    tags: str
    notes: Optional[str]
    flagged: bool
    flag_reason: Optional[str]

class ImportResult(BaseModel):
    file: str
    platform_detected: Optional[str] = None
    account_id: Optional[str] = None
    imported: int = 0
    duplicates: int = 0
    skipped: int = 0
    warnings: List[str] = []

class ImportResponse(BaseModel):
    results: List[ImportResult]

class AccountOut(BaseModel):
    id: str
    platform: str
    transaction_count: int
    min_date: Optional[str]
    max_date: Optional[str]

class HealthResponse(BaseModel):
    status: str

class SankeyNode(BaseModel):
    id: str
    label: str
    value_cents: int
    color: str
    col: int
    kind: str

class SankeyLink(BaseModel):
    source: str
    target: str
    value_cents: int

class SankeyResponse(BaseModel):
    nodes: list[SankeyNode]
    links: list[SankeyLink]
    total_income_cents: int
    total_expense_cents: int
    net_cents: int

class TransferOut(BaseModel):
    id: int
    from_transaction_id: int
    to_transaction_id: int
    matched_by: str

class ManualTransactionCreate(BaseModel):
    amount_cents: int
    posted_date: str
    category_path: Optional[str] = None
    merchant: str = ""
    description: str = ""
    business_context: str = "personal"
    notes: Optional[str] = None
    account_id: str = "manual"
    source_platform: str = "manual"
    source_file: str = "manual"

class TransactionPatch(BaseModel):
    category_path: Optional[str] = None
    apply_to_matching: bool = False
    tags: Optional[List[str]] = None

class CorrectionResponse(BaseModel):
    re_categorized_count: int
    affected_ids: List[int]
    context_diff: Optional[str] = None
    before: dict
    after: dict

class BulkEditPatch(BaseModel):
    category_path: Optional[str] = None
    business_context: Optional[str] = None
    tags: Optional[List[str]] = None
    notes: Optional[str] = None

class BulkEditRequest(BaseModel):
    ids: List[int]
    patch: BulkEditPatch

class BulkEditResponse(BaseModel):
    """``skipped_*`` describes rows the batch deliberately left alone.

    A split parent (a combined charge whose item rows carry the real categories)
    cannot take a spending category without double-counting, so it is skipped
    instead of failing the whole batch — the user's selection was meant for the
    ordinary rows. Reporting it keeps ``updated_count`` from silently coming back
    smaller than the selection with no explanation.
    """
    updated_count: int
    updated_ids: List[int]
    skipped_ids: List[int] = []
    skipped_count: int = 0
    skipped_reason: Optional[str] = None

class ApplySimilarRequest(BaseModel):
    """Propagate one manual categorization to the rows naming the same merchant.

    ``ids`` is what the user ticked in the review list; ``all_matching`` says
    "everything you found, including what the list truncated" and makes ``ids``
    irrelevant. Either way the server re-derives the match set — the client's
    view can be stale, and this endpoint must not become a way to write an
    arbitrary category onto an arbitrary row.
    """
    source_id: int
    category_path: str
    ids: List[int] = []
    all_matching: bool = False
    create_rule: bool = False

class ApplySimilarResponse(BaseModel):
    updated_count: int
    updated_ids: List[int]
    skipped_count: int
    rule_id: Optional[int] = None
    context_updated: bool = False

class ChatCorrectRequest(BaseModel):
    message: str

class ChatCorrectResponse(BaseModel):
    affected_count: int
    affected_ids: List[int]
    summary: str
    context_diff: Optional[str] = None

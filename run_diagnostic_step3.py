import sys, os, io, json
sys.path.insert(0, 'src')
sys.stdout.reconfigure(encoding='utf-8')

print('=== STEP 3: LLM CONNECTION & EXTRACTION TRACING ===')
from doc_extractor.pipeline import ModuleAPipeline
from doc_extractor.pipeline_b import ModuleBPipeline
from doc_extractor.pipeline_c import ModuleCPipeline
from doc_extractor.extractor import SchemaGuidedExtractor
from doc_extractor.schemas import CASASchema

img_path = 'C:/Users/ddnan/.gemini/antigravity/brain/8f7911e7-56eb-4264-8921-aad1a2011ba0/.user_uploaded/media_1794109987840_646d3121.jpg'
raw_bytes = open(img_path, 'rb').read()

ma = ModuleAPipeline()
stream = io.BytesIO(raw_bytes)
handoff = ma.process_document(stream, 'test_account_form.jpg')
mb = ModuleBPipeline(storage=ma.storage)
out_b = mb.process_handoff(handoff)

# Check default extractor configuration
extractor_default = SchemaGuidedExtractor()
print(f'Default extractor use_llm: {extractor_default.use_llm}')
print(f'Default extractor allow_fallback: {extractor_default.allow_fallback}')
print(f'Default extractor model_name: {extractor_default.model_name}')
print(f'Default extractor provider_name: {extractor_default.provider_name}')

# Try LLM execution with use_llm=True to inspect prompt & error
print('\n--- TESTING LLM EXECUTION (use_llm=True) ---')
extractor_llm = SchemaGuidedExtractor(use_llm=True, allow_fallback=True)

# Reconstruct system & user prompt exactly as done in _extract_llm_internal
sys_prompt = (
    "You are a schema-constrained document extraction engine. "
    "Extract only the fields defined in the schema from the provided document text. "
    "If a field is absent or not legibly visible, set it to null. "
    "Never infer, guess, or hallucinate values. "
    "Return strictly valid JSON matching the schema."
)
user_prompt = out_b.combined_markdown

print('Prompt Sent to Model:')
print(f'  System: {sys_prompt}')
print(f'  User content length: {len(user_prompt)} characters')
print(f'  User content preview:\n{user_prompt}\n')

# Check what happens during actual LLM call
try:
    model_res, req_id = extractor_llm._extract_llm_internal(out_b, CASASchema)
    print(f'LLM call succeeded! Response: {model_res}')
except Exception as exc:
    print(f'LLM call raised exception: {type(exc).__name__}: {str(exc)}')

# Test complete Module C pipeline execution
print('\n--- TESTING MODULE C PIPELINE EXECUTION ---')
mc = ModuleCPipeline()
envelope = mc.process(out_b)
print(f'Final Document Status: {envelope.document_metadata.status}')
print(f'Flags Raised: {[(f.code, f.message, f.fields) for f in envelope.flags]}')
print(f'Provenance: {envelope.run.provenance.model_dump() if hasattr(envelope.run, "provenance") and envelope.run.provenance else None}')

print('\nFields Summary:')
for f_name, f_data in envelope.fields.items():
    val = f_data.value
    rev = f_data.review_required
    ev_bbox = f_data.evidence.bbox if f_data.evidence else None
    print(f'  {f_name:40s} | val={str(val):20s} | rev={str(rev):5s} | ev_bbox={str(ev_bbox)}')


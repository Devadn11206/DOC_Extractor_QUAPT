import sys, os, io, json
sys.path.insert(0, 'src')
sys.stdout.reconfigure(encoding='utf-8')
import cv2

print('=== STEP 2: OCR & LAYOUT PARSER INSPECTION ===')
from doc_extractor.pipeline import ModuleAPipeline
from doc_extractor.pipeline_b import ModuleBPipeline

img_path = 'C:/Users/ddnan/.gemini/antigravity/brain/8f7911e7-56eb-4264-8921-aad1a2011ba0/.user_uploaded/media_1794109987840_646d3121.jpg'
raw_bytes = open(img_path, 'rb').read()

ma = ModuleAPipeline()
stream = io.BytesIO(raw_bytes)
handoff = ma.process_document(stream, 'test_account_form.jpg')

mb = ModuleBPipeline(storage=ma.storage)
load_res = mb.loader.load_handoff(handoff)
page = load_res.pages[0]

# Inspect parser configuration and adapter
parser = mb.parser
print(f'Parser class: {parser.__class__.__name__}')
print(f'Engine name: {parser.engine_name}')
print(f'Configured raw extractor: {parser._raw_extractor}')

# Execute parse_page
parsed_page = parser.parse_page(page)
print(f'Total Layout Blocks / OCR Tokens Detected: {len(parsed_page.blocks)}')

# Inspect detected blocks
for idx, b in enumerate(parsed_page.blocks[:15]):
    print(f'  Block [{idx:02d}]: {repr(b.text)} | bbox={b.bbox_original.to_tuple() if b.bbox_original else None} | conf={b.ocr_confidence:.2f}')

print('\n--- GENERATED LAYOUT MARKDOWN (FIRST 30 LINES) ---')
md_lines = parsed_page.markdown_content.splitlines()
for i, line in enumerate(md_lines[:30]):
    print(f'{i:02d}: {line}')

# Check classification stage
out_b = mb.process_handoff(handoff)
print('\n--- MODULE B CLASSIFICATION OUTPUT ---')
print(f'Document Type: {out_b.classification.document_type.value}')
print(f'Domain: {out_b.classification.domain.value}')
print(f'Confidence: {out_b.classification.classification_confidence}')
print(f'Review Required: {out_b.classification.review_required}')
print(f'Classification Reasons: {out_b.classification.classification_reasons}')


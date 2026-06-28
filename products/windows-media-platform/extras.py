"""Per-product extension fields for the Windows Media Platform product.

Composed onto CoreClassification at runtime. Add fields here for anything
this product needs the LLM to fill that isn't already in CoreClassification.
Leave the class empty if you don't need any extras.
"""

from __future__ import annotations

from pydantic import BaseModel


class ProductExtras(BaseModel):
    pass

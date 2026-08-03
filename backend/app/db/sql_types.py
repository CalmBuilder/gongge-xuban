"""
@Time       : 2026/07/18 09:22
@Author     : zhanglp8181
@File       : sql_types.py
@CallChain  : models.py → semantic SQL types → SQLAlchemy dialect compiler
@Description: 定义跨 SQLite 和 MySQL 的语义化字符串与文本类型。
"""

from typing import Annotated, Optional

from sqlalchemy import DateTime, Text
from sqlalchemy.dialects.mysql import DATETIME, LONGTEXT, MEDIUMTEXT
from sqlmodel import Field

PRIMARY_KEY_LENGTH = 512
IDENTIFIER_LENGTH = 128
NAME_LENGTH = 191
LABEL_LENGTH = 64
VERSION_LENGTH = 64

MEDIUM_TEXT = Text().with_variant(MEDIUMTEXT(), "mysql")
LONG_TEXT = Text().with_variant(LONGTEXT(), "mysql")
PRECISE_DATETIME = DateTime().with_variant(DATETIME(fsp=6), "mysql")

PrimaryKeyString = Annotated[str, Field(max_length=PRIMARY_KEY_LENGTH)]
IdentifierString = Annotated[str, Field(max_length=IDENTIFIER_LENGTH)]
OptionalIdentifierString = Annotated[Optional[str], Field(max_length=IDENTIFIER_LENGTH)]
NameString = Annotated[str, Field(max_length=NAME_LENGTH)]
OptionalNameString = Annotated[Optional[str], Field(max_length=NAME_LENGTH)]
LabelString = Annotated[str, Field(max_length=LABEL_LENGTH)]
OptionalLabelString = Annotated[Optional[str], Field(max_length=LABEL_LENGTH)]
VersionString = Annotated[str, Field(max_length=VERSION_LENGTH)]
OptionalVersionString = Annotated[Optional[str], Field(max_length=VERSION_LENGTH)]
PasswordHashString = Annotated[str, Field(max_length=255)]
LongTextString = Annotated[str, Field(sa_type=LONG_TEXT)]
OptionalLongTextString = Annotated[Optional[str], Field(sa_type=LONG_TEXT)]
MediumTextString = Annotated[str, Field(sa_type=MEDIUM_TEXT)]
OptionalMediumTextString = Annotated[Optional[str], Field(sa_type=MEDIUM_TEXT)]
PlainTextString = Annotated[str, Field(sa_type=Text)]
OptionalPlainTextString = Annotated[Optional[str], Field(sa_type=Text)]

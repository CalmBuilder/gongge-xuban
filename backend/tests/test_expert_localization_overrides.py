from app.experts.localization_integrity import validate_translation
from app.experts.localization_overrides import LINE_TRANSLATION_OVERRIDES


def test_manual_line_overrides_are_exact_and_pass_fidelity_validation() -> None:
    assert len(LINE_TRANSLATION_OVERRIDES) == 6
    for source, translated in LINE_TRANSLATION_OVERRIDES.items():
        assert validate_translation(source, translated).valid is True

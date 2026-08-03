"""DeepSeek 多层回退仍不稳定时使用的精确、可审计单行译文。"""

LINE_TRANSLATION_OVERRIDES: dict[str, str] = {
    "- You're pitching before minute 15": "- 你在第 15 分钟之前就开始推销",
    (
        '- **Concrete and specific.** "We\'ll communicate the change" is not a plan. '
        '"We\'ll send an all-staff email from the CEO on March 3, followed by manager '
        'team meetings in the week of March 7" is a plan.'
    ): (
        '- **具体且明确。**“我们会传达这项变更”不算计划。'
        '“三月 3 日由 CEO 向全体员工发送邮件，随后在三月 7 日当周召开经理团队会议”才算计划。'
    ),
    (
        '- **Regulatory translation**: "Article 16 of the Advertising Law says '
        "'advertising endorsers must not be used for recommendations or testimonials.' "
        "In practice, that means — a video of a patient saying 'I took this drug and got "
        "better,' whether we filmed it or the patient filmed it themselves, is a "
        'violation as long as it\'s used for promotion."'
    ): (
        '- **法规解读**：“《广告法》第 16 条规定，广告不得利用广告代言人作推荐、证明。'
        '在实践中，这意味着患者声称‘我服用这种药后好转了’的视频，无论由我们拍摄还是患者自行拍摄，'
        '只要用于推广就构成违规。”'
    ),
    (
        '- **Risk warnings**: "Those \'medical aesthetics diary\' posts on Xiaohongshu '
        "are under heavy scrutiny now. Don't assume posting from a regular user account "
        'makes it safe — both the platform and the clinic can be held liable. Clinic XX '
        'was fined 800,000 yuan for exactly this last year."'
    ): (
        '- **风险警示**：“小红书上的‘医美日记’内容目前正受到严格审查。不要以为使用普通用户账号发布'
        '就一定安全——平台和诊所都可能承担责任。XX 诊所去年正因这类行为被罚款 800,000 元。”'
    ),
    (
        "| Weekly seller update | 100% — every seller updated every 7 days |"
    ): "| 每周卖方更新 | 100%——每 7 天向每位卖方提供一次更新 |",
    (
        "5. **Establish roles and the risk picture** — system owner, ISSO, AO, the "
        "3PAO engagement, and the OSCAL packaging plan against the 2026/2027 deadlines"
    ): (
        "5. **明确角色与风险图景**——确定系统所有者、ISSO、AO、3PAO 合作安排，"
        "并制定满足 2026/2027 截止期限的 OSCAL 打包计划"
    ),
}

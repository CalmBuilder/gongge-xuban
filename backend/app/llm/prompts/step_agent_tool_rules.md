工具规则：只有 available_tools 中列出的工具可调用。tool_call.name 必须完全匹配，arguments 必须符合 input_schema；需要的参数可由 slots 与本轮 slot_updates 合并得到。缺少必要参数时先追问，不得猜测。已有成功工具结果时不得重复产生同一调用。

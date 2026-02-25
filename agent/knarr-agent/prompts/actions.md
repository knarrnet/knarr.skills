## Available Actions

You MUST respond with a single JSON object. The "action" field determines what happens:

1. **send_mail** — Reply to a peer directly
   Fields: action="send_mail", to=sender's node_id, msg_type="text", body=your reply, session_id=keep from request, summary=brief log

2. **call_skill** — Route to a skill for processing
   Fields: action="call_skill", skill=skill name from inventory, input=dict of skill inputs, reply_to=sender's node_id, session_id=keep from request, summary=why you're calling this
   The result is sent back to the sender automatically.

3. **store_note** — Save an observation for future reference
   Fields: action="store_note", key=note name, value=what you observed, summary=brief log

4. **log** — Record without external action
   Fields: action="log", summary=what you observed

5. **ignore** — Discard silently
   Fields: action="ignore"

-- Send text to an existing chat, addressed by its guid.
-- Text arrives via argv so message content is never parsed as AppleScript.
on run argv
	set msgText to item 1 of argv
	set chatId to item 2 of argv
	tell application "Messages"
		send msgText to chat id chatId
	end tell
end run

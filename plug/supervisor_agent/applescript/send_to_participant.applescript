-- Fallback: address a participant on a named service (iMessage / SMS).
on run argv
	set msgText to item 1 of argv
	set targetHandle to item 2 of argv
	set svc to item 3 of argv
	tell application "Messages"
		set theAccount to first account whose service type = service type svc
		send msgText to participant targetHandle of theAccount
	end tell
end run

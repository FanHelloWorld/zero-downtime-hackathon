-- Last-resort fallback for older addressing behaviour.
on run argv
	set msgText to item 1 of argv
	set targetHandle to item 2 of argv
	tell application "Messages"
		send msgText to buddy targetHandle
	end tell
end run

# Local scheduling (free)

## macOS (`launchd`)

Create `~/Library/LaunchAgents/com.pulse.weekly.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key><string>com.pulse.weekly</string>
    <key>ProgramArguments</key>
    <array>
      <string>/bin/zsh</string>
      <string>-lc</string>
      <string>cd "/path/to/Project 3 - App Reviews Insights Analyzer" && uv run pulse run --product groww</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
      <key>Weekday</key><integer>2</integer>
      <key>Hour</key><integer>7</integer>
      <key>Minute</key><integer>0</integer>
    </dict>
    <key>StandardOutPath</key><string>/tmp/pulse-weekly.out</string>
    <key>StandardErrorPath</key><string>/tmp/pulse-weekly.err</string>
    <key>RunAtLoad</key><true/>
  </dict>
</plist>
```

Load:

```bash
launchctl load ~/Library/LaunchAgents/com.pulse.weekly.plist
```

## Linux (`cron`)

Run each Monday 07:00:

```cron
0 7 * * 1 cd "/path/to/Project 3 - App Reviews Insights Analyzer" && uv run pulse run --product groww >> /tmp/pulse-weekly.out 2>> /tmp/pulse-weekly.err
```

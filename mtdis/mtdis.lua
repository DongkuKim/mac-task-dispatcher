-- mtdis: Hammerspoon side.
-- Watches network, power and wake events (plus a periodic timer) and runs
-- `mtdis run` with the current state. `mtdis setup` adds this to
-- ~/.hammerspoon/init.lua:
--
--   require("mtdis").start({ command = "/opt/homebrew/bin/mtdis" })
--
-- Options: command (path to mtdis, default /opt/homebrew/bin/mtdis),
--          interval (seconds between timer runs, default 60; 0 disables the timer).

local M = {}

-- Watchers are kept on the module so they aren't garbage-collected (which would
-- silently stop them).
M.watchers = {}

local function isAC()
    return hs.battery.powerSource() == "AC Power"
end

local function isReachable(flags)
    return (flags & hs.network.reachability.flags.reachable) ~= 0
end

function M.start(opts)
    opts = opts or {}
    local command = opts.command or "/opt/homebrew/bin/mtdis"
    local interval = opts.interval or 60

    local reach = hs.network.reachability.internet()
    local state = { internet = isReachable(reach:status()), ac = isAC() }
    local current, rerun = nil, nil

    -- Runs asynchronously so Hammerspoon never freezes while the dispatcher samples
    -- CPU or waits for a task to stop. Calls that arrive mid-run are merged into one
    -- follow-up run.
    local function dispatch(reason)
        if current and current:isRunning() then
            rerun = reason
            return
        end
        current = hs.task.new(command, function(code, stdout, stderr)
            if code ~= 0 then
                print("mtdis failed (" .. code .. "): " .. stderr)
            end
            current = nil
            if rerun then
                local r = rerun
                rerun = nil
                dispatch(r)
            end
        end, { "run",
               "--internet", tostring(state.internet),
               "--ac", tostring(state.ac),
               "--reason", reason })
        current:start()
    end
    M.dispatch = dispatch

    M.watchers.reach = reach:setCallback(function(_, flags)
        local now = isReachable(flags)
        if now ~= state.internet then
            state.internet = now
            dispatch(now and "online" or "offline")
        end
    end):start()

    M.watchers.battery = hs.battery.watcher.new(function()
        local now = isAC()
        if now ~= state.ac then
            state.ac = now
            dispatch(now and "ac-power" or "battery")
        end
    end):start()

    M.watchers.wake = hs.caffeinate.watcher.new(function(event)
        if event == hs.caffeinate.watcher.systemDidWake then
            state.ac = isAC()
            state.internet = isReachable(reach:status())
            dispatch("wake")
        end
    end):start()

    -- Events alone miss slow changes (a task's memory growing, a task finishing),
    -- so the timer re-checks regularly. Each run costs about one second of sampling.
    if interval > 0 then
        M.watchers.timer = hs.timer.doEvery(interval, function() dispatch("timer") end)
    end

    dispatch("startup")
    return M
end

return M

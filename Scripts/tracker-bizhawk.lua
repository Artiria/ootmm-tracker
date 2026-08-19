-- tracker-bizhawk.lua - memory server for the OoTMM autotracker, BizHawk side.
--
-- Talks to the tracker over shared memory (comm.mmf*), NOT the Lua socket.
-- The socket only exists when EmuHawk is started with --socket flags, which
-- would force the tracker to relaunch the emulator; the memory-mapped files
-- are always there, so this is the Project64 flow: open EmuHawk with the seed,
-- load this script, done. No relaunch, no flags.
--
-- A memory-mapped file has no signalling, so the two sides poll and take
-- turns. Three mappings, all created by the tracker (Python), opened here by
-- name (see mmflink.py for the full contract):
--
--   OoTMMTracker.req  (tracker -> here): u32 seq @0 | u8 op @4 | u32 addr @8 | u32 len @12
--   OoTMMTracker.resp (here -> tracker): u32 len @0 | bytes @4
--   OoTMMTracker.seq  (here -> tracker): u32 seq @0
--
-- Each frame: read .req; if its seq is new, serve op (P -> 'BIZ1', B -> the
-- bytes at addr in real memory order), write .resp, then .seq. Writing the
-- data before the seq -- and the seq in its own mapping -- is what lets the
-- tracker trust the data behind a seq it just saw.
--
-- No bit library on purpose: addresses are 0x8xxxxxxx, above 2^31, where
-- 32-bit bitwise helpers quietly go wrong.

local NAME = "OoTMMTracker"
local REQ = NAME .. ".req"
local RESP = NAME .. ".resp"
local SEQ = NAME .. ".seq"

local REQ_SIZE = 32
local OP_PING = 0x50       -- 'P'
local OP_BLOCK = 0x42      -- 'B'
local MAX_BLOCK = 0x10000
local CAL_ADDR = 0x318     -- osMemSize, physical: never 0, never a palindrome

local swap = false         -- filled in by calibrate()
local have_array = true    -- read_bytes_as_array present?

local function phys(addr)
  return addr % 0x20000000
end

-- u32 little-endian from a 0-indexed byte table (mmfReadBytes returns those)
local function u32le(t, off)
  return t[off] + t[off + 1] * 0x100 + t[off + 2] * 0x10000 + t[off + 3] * 0x1000000
end

-- 4 little-endian bytes of v, appended to a 1-indexed table at position p
local function put_u32le(t, p, v)
  t[p] = v % 0x100
  t[p + 1] = math.floor(v / 0x100) % 0x100
  t[p + 2] = math.floor(v / 0x10000) % 0x100
  t[p + 3] = math.floor(v / 0x1000000) % 0x100
end

local function read_array(addr, n)
  if have_array then
    local ok, t = pcall(mainmemory.read_bytes_as_array, addr, n)
    if ok and t and t[1] ~= nil then
      return t
    end
    have_array = false
    print('tracker-bizhawk: read_bytes_as_array unavailable, using word reads')
  end
  local t = {}
  local i = 0
  while i < n do
    local v = mainmemory.read_u32_be(addr + i)
    t[i + 1] = math.floor(v / 0x1000000) % 0x100
    t[i + 2] = math.floor(v / 0x10000) % 0x100
    t[i + 3] = math.floor(v / 0x100) % 0x100
    t[i + 4] = v % 0x100
    i = i + 4
  end
  return t
end

-- N64 byte order or word-swapped? Ask the core, do not assume (BizHawk's N64
-- domains have been byte-swapped before).
local function calibrate()
  local v = mainmemory.read_u32_be(CAL_ADDR)
  local b = read_array(CAL_ADDR, 4)
  if not have_array then
    swap = false
    return v
  end
  local straight = b[1] * 0x1000000 + b[2] * 0x10000 + b[3] * 0x100 + b[4]
  local reversed = b[4] * 0x1000000 + b[3] * 0x10000 + b[2] * 0x100 + b[1]
  if straight == v then
    swap = false
  elseif reversed == v then
    swap = true
  else
    print('tracker-bizhawk: WARNING - cannot tell byte order at 0x' ..
          string.format('%08X', CAL_ADDR) .. '; assuming N64 order')
    swap = false
  end
  return v
end

-- Build the .resp payload (1-indexed table) for a block read: u32 len, bytes.
local function block_payload(addr, n)
  local t = read_array(addr, n)
  if swap then
    local i = 1
    while i + 3 <= n do
      t[i], t[i + 1], t[i + 2], t[i + 3] = t[i + 3], t[i + 2], t[i + 1], t[i]
      i = i + 4
    end
  end
  local out = {}
  put_u32le(out, 1, n)
  for i = 1, n do
    out[4 + i] = t[i]
  end
  return out
end

local function serve(op, addr, len)
  if op == OP_PING then
    return { 4, 0, 0, 0, 0x42, 0x49, 0x5A, 0x31 }   -- len 4, 'BIZ1'
  elseif op == OP_BLOCK then
    if len > MAX_BLOCK then
      len = MAX_BLOCK
    end
    return block_payload(phys(addr), len)
  end
  return { 0, 0, 0, 0 }   -- len 0
end

-- Wait for a running ROM: calibrating a dead core throws and would leave the
-- tracker hanging. This is the failure this project keeps meeting.
local size = nil
local said = false
while true do
  have_array = true
  local ok, v = pcall(calibrate)
  if ok and (v == 0x400000 or v == 0x800000) then
    size = v
    break
  end
  if not said then
    print('tracker-bizhawk: waiting for a running ROM (osMemSize not readable yet)')
    said = true
  end
  emu.frameadvance()
end

print('tracker-bizhawk: RDRAM ' .. string.format('%.0f', size / 1048576) .. ' MB, ' ..
      (swap and 'word-swapped domain (undoing it)' or 'N64 byte order') ..
      (have_array and '' or ', word reads'))

-- Wait for the tracker to have created the mappings (.req OpenExisting throws
-- until then), so loading this before the tracker is running just waits.
local said_wait = false
while true do
  if pcall(comm.mmfReadBytes, REQ, REQ_SIZE) then
    break
  end
  if not said_wait then
    print('tracker-bizhawk: waiting for the tracker (start it, it makes the shared memory)')
    said_wait = true
  end
  emu.frameadvance()
end
print('tracker-bizhawk: serving over shared memory "' .. NAME .. '"')

local last = 0
while true do
  local ok, req = pcall(comm.mmfReadBytes, REQ, REQ_SIZE)
  if ok and req ~= nil then
    local seq = u32le(req, 0)
    if seq ~= last then
      local op = req[4]
      local addr = u32le(req, 8)
      local len = u32le(req, 12)
      local fine, payload = pcall(serve, op, addr, len)
      if fine then
        comm.mmfWriteBytes(RESP, payload)   -- data first
        comm.mmfWriteBytes(SEQ, { seq % 0x100, math.floor(seq / 0x100) % 0x100,
                                  math.floor(seq / 0x10000) % 0x100,
                                  math.floor(seq / 0x1000000) % 0x100 })  -- seq after
        last = seq
      else
        print('tracker-bizhawk: ' .. tostring(payload))
      end
    end
  end
  emu.frameadvance()
end

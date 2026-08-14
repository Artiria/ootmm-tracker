-- tracker.lua - memory server for the OoTMM autotracker.
--
-- A standalone script. It does not replace adapter.lua: it uses a port of its
-- own (13251) so both can run at the same time.
--
--   Single player:  this script alone.
--   Multi:          this script + the multi's adapter.lua, side by side.
--
-- On the other end ootmm.py listens (watch / dump / items / overlay). This
-- script is the client, because P64-EM's Lua socket can only connect, not
-- listen.
--
-- Protocol: identical to adapter.lua's on opcodes 2/3/4/6/7/8, plus PING and
-- READ_BLOCK. Big endian, like the rest of the emulator.
--
--   0x01 PING                            -> u32 magic 'TRK1'
--   0x02 READ_8      addr:u32            -> u8
--   0x03 READ_16     addr:u32            -> u16
--   0x04 READ_32     addr:u32            -> u32
--   0x06 WRITE_8     addr:u32 val:u8
--   0x07 WRITE_16    addr:u32 val:u16
--   0x08 WRITE_32    addr:u32 val:u32
--   0x10 READ_BLOCK  addr:u32 len:u32    -> len bytes  (addr and len multiples of 4)

local HOST = 'localhost'
local PORT = 13251

local MAGIC = 0x54524B31       -- 'TRK1'
local MAX_BLOCK = 0x10000      -- 64 KB per request
local CHUNK = 4096             -- send chunk

local s = nil

-- s:recv(n) can return fewer than n bytes; keep at it until complete.
local function recv_exact(n)
  local buf = ''
  while #buf < n do
    local part = s:recv(n - #buf)
    if part == nil or #part == 0 then
      error('disconnected')
    end
    buf = buf .. part
  end
  return buf
end

local function send_all(data)
  local i = 1
  while i <= #data do
    s:send(data:sub(i, i + CHUNK - 1))
    i = i + CHUNK
  end
end

local function connect()
  while true do
    s = socket.tcp(HOST, PORT)
    if s ~= nil then
      print('tracker.lua connected to ' .. HOST .. ':' .. PORT)
      return
    end
    print('tracker.lua: waiting for the daemon at ' .. HOST .. ':' .. PORT)
    socket.sleep(1)
  end
end

local function read_block()
  local addr = binary.unpack_u32(recv_exact(4))
  local len = binary.unpack_u32(recv_exact(4))
  if len > MAX_BLOCK then
    len = MAX_BLOCK
  end
  local parts = {}
  local i = 0
  while i < len do
    parts[#parts + 1] = binary.pack_u32(memory.read_u32(addr + i))
    i = i + 4
  end
  send_all(table.concat(parts))
end

local function serve()
  while true do
    local op = binary.unpack_u8(recv_exact(1))

    if op == 1 then
      s:send(binary.pack_u32(MAGIC))

    elseif op == 2 then
      local addr = binary.unpack_u32(recv_exact(4))
      s:send(binary.pack_u8(memory.read_u8(addr)))

    elseif op == 3 then
      local addr = binary.unpack_u32(recv_exact(4))
      s:send(binary.pack_u16(memory.read_u16(addr)))

    elseif op == 4 then
      local addr = binary.unpack_u32(recv_exact(4))
      s:send(binary.pack_u32(memory.read_u32(addr)))

    elseif op == 6 then
      local addr = binary.unpack_u32(recv_exact(4))
      memory.write_u8(addr, binary.unpack_u8(recv_exact(1)))

    elseif op == 7 then
      local addr = binary.unpack_u32(recv_exact(4))
      memory.write_u16(addr, binary.unpack_u16(recv_exact(2)))

    elseif op == 8 then
      local addr = binary.unpack_u32(recv_exact(4))
      memory.write_u32(addr, binary.unpack_u32(recv_exact(4)))

    elseif op == 16 then
      read_block()

    else
      -- An unknown opcode desyncs the stream: cut and reconnect.
      error('unknown opcode: ' .. op)
    end
  end
end

-- Outer loop: survives daemon restarts without having to restart the
-- emulator or the ROM.
while true do
  connect()
  local ok, err = pcall(serve)
  if not ok then
    print('tracker.lua: session ended (' .. tostring(err) .. '), reconnecting')
  end
  pcall(function() s:close() end)
  s = nil
  socket.sleep(1)
end

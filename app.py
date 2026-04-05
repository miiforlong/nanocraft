import pygame
from pygame.locals import *
from OpenGL.GL import *
from OpenGL.GLU import *
import numpy as np
import math
import socket
import threading
import pickle
import struct
import zlib
import time
import os
import json
import uuid
from concurrent.futures import ThreadPoolExecutor

# --- CONFIGURATION ---
WIDTH, HEIGHT = 1024, 768
CHUNK_SIZE = 16
MAP_W, MAP_D, MAP_H = 128, 64, 128
RENDER_DIST = 96
JAVA_RENDER_DIST_CHUNKS = 3
JAVA_VERTICAL_RENDER_CHUNKS = 2
JAVA_RENDER_KEEP_CHUNKS = 1
JAVA_PREFETCH_CHUNKS = 1
JAVA_LOADING_MIN_CHUNKS = 96
JAVA_LOADING_IDLE_SECONDS = 3.0
JAVA_LOADING_BUILD_BUDGET = 4
JAVA_REBUILD_BUDGET = 1
JAVA_LOADING_FRAME_BUDGET_MS = 5.0
JAVA_REBUILD_FRAME_BUDGET_MS = 2.5
JAVA_BLOCK_UPDATE_INTERVAL = 0.08
JAVA_BLOCK_BURST_SECONDS = 0.25
JAVA_BLOCK_REBUILD_BUDGET = 2
JAVA_CHUNK_WORKERS = max(2, min(8, os.cpu_count() or 4))
JAVA_CHUNK_QUEUE_LIMIT = 128
JAVA_LAN_PORT_DEFAULT = 25565
JAVA_LAN_MULTICAST_GROUP = "224.0.2.60"
JAVA_LAN_MULTICAST_PORT = 4445
PORT = 5555

# =====================================================================
# PROTOCOLE MINECRAFT JAVA (1.8 / Protocole 47)
# =====================================================================

def read_varint(data, offset=0):
    """Lit un VarInt depuis des bytes, retourne (valeur, nouveau_offset)"""
    result = 0
    shift = 0
    while True:
        if offset >= len(data):
            raise ValueError("VarInt tronquÃ©")
        b = data[offset]
        offset += 1
        result |= (b & 0x7F) << shift
        shift += 7
        if not (b & 0x80):
            break
        if shift >= 35:
            raise ValueError("VarInt trop long")
    return result, offset

def write_varint(value):
    """Encode un int en VarInt bytes"""
    out = bytearray()
    value = value & 0xFFFFFFFF
    while True:
        part = value & 0x7F
        value >>= 7
        if value:
            out.append(part | 0x80)
        else:
            out.append(part)
            break
    return bytes(out)

def write_string(s):
    """Encode une chaÃ®ne UTF-8 avec prefix VarInt"""
    encoded = s.encode('utf-8')
    return write_varint(len(encoded)) + encoded

def read_string(data, offset):
    """Lit une chaÃ®ne UTF-8 avec prefix VarInt"""
    length, offset = read_varint(data, offset)
    s = data[offset:offset+length].decode('utf-8')
    return s, offset + length

def read_angle(data, offset):
    return data[offset] * 360.0 / 256.0, offset + 1

def pack_block_position(x, y, z):
    value = ((int(x) & 0x3FFFFFF) << 38) | ((int(y) & 0xFFF) << 26) | (int(z) & 0x3FFFFFF)
    if value >= (1 << 63):
        value -= (1 << 64)
    return struct.pack('>q', value)

def write_slot(item_id=-1, count=0, damage=0):
    if item_id < 0:
        return struct.pack('>h', -1)
    return struct.pack('>hb', item_id, count) + struct.pack('>h', damage) + b'\x00'

def write_packet(packet_id, payload, compression_threshold=-1):
    """Encode un paquet complet avec support optionnel de la compression."""
    id_bytes = write_varint(packet_id)
    data = id_bytes + payload

    if compression_threshold < 0:
        # Pas de compression : length + data
        return write_varint(len(data)) + data
    else:
        # Compression activÃ©e
        if len(data) >= compression_threshold:
            # Compresser
            compressed = zlib.compress(data)
            data_length = write_varint(len(data))   # taille dÃ©compressÃ©e
            inner = data_length + compressed
        else:
            # Trop petit : data_length = 0 signifie "non compressÃ©"
            inner = write_varint(0) + data
        return write_varint(len(inner)) + inner

def recv_exact(sock, n):
    """Lit exactement n bytes depuis le socket"""
    buf = b''
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("Connexion fermÃ©e")
        buf += chunk
    return buf

def recv_packet(sock, compression_threshold=-1):
    """Lit un paquet complet depuis le socket"""
    # Lire la longueur du paquet
    raw = b''
    while True:
        b = sock.recv(1)
        if not b:
            raise ConnectionError("Connexion fermÃ©e")
        raw += b
        if not (b[0] & 0x80):
            break
        if len(raw) > 5:
            raise ValueError("Longueur trop grande")
    
    packet_len, _ = read_varint(raw)
    data = recv_exact(sock, packet_len)
    
    if compression_threshold >= 0:
        # Compression activÃ©e
        data_len, off = read_varint(data)
        if data_len > 0:
            data = zlib.decompress(data[off:])
        else:
            data = data[off:]
    
    packet_id, off = read_varint(data)
    return packet_id, data, off

# =====================================================================
# DÃ‰CODAGE DES CHUNKS MINECRAFT 1.8
# =====================================================================

# Table de mapping block_id -> couleur RGB (pour l'affichage OpenGL)
BLOCK_COLORS = {
    0:  None,           # Air
    1:  (0.5, 0.5, 0.5),  # Stone
    2:  (0.3, 0.6, 0.2),  # Grass
    3:  (0.55, 0.35, 0.1),# Dirt
    4:  (0.6, 0.6, 0.6),  # Cobblestone
    5:  (0.7, 0.5, 0.3),  # Wood Plank
    7:  (0.2, 0.2, 0.2),  # Bedrock
    8:  (0.2, 0.3, 0.8),  # Water flowing
    9:  (0.2, 0.3, 0.8),  # Water
    10: (0.9, 0.4, 0.0),  # Lava
    11: (0.9, 0.4, 0.0),  # Lava
    12: (0.85, 0.8, 0.55),# Sand
    13: (0.5, 0.5, 0.5),  # Gravel
    14: (0.5, 0.5, 0.3),  # Gold ore
    15: (0.5, 0.5, 0.5),  # Iron ore
    16: (0.3, 0.3, 0.3),  # Coal ore
    17: (0.5, 0.35, 0.15),# Log
    18: (0.2, 0.6, 0.2),  # Leaves
    20: (0.7, 0.9, 1.0),  # Glass
    24: (0.85, 0.8, 0.55),# Sandstone
    31: (0.3, 0.7, 0.2),  # Tallgrass
    35: (0.9, 0.9, 0.9),  # Wool
    41: (0.9, 0.8, 0.2),  # Gold block
    42: (0.7, 0.7, 0.7),  # Iron block
    43: (0.6, 0.6, 0.6),  # Double slab
    44: (0.6, 0.6, 0.6),  # Slab
    45: (0.7, 0.4, 0.3),  # Bricks
    48: (0.3, 0.4, 0.3),  # Mossy cobblestone
    49: (0.15, 0.1, 0.25),# Obsidian
    52: (0.1, 0.1, 0.3),  # Spawner
    53: (0.7, 0.5, 0.3),  # Oak stairs
    56: (0.4, 0.7, 0.8),  # Diamond ore
    57: (0.5, 0.9, 0.9),  # Diamond block
    73: (0.5, 0.2, 0.2),  # Redstone ore
    74: (0.5, 0.2, 0.2),  # Redstone ore glowing
    78: (0.95, 0.95, 1.0),# Snow
    79: (0.7, 0.85, 0.95),# Ice
    80: (0.95, 0.95, 1.0),# Snow block
    82: (0.6, 0.6, 0.7),  # Clay
    86: (0.85, 0.45, 0.1),# Pumpkin
    87: (0.7, 0.3, 0.2),  # Netherrack
    89: (0.9, 0.8, 0.5),  # Glowstone
    98: (0.6, 0.6, 0.6),  # Stone brick
    116:(0.3, 0.2, 0.5),  # Enchanting table
}

JAVA_NON_CUBE_BLOCKS = {
    6, 8, 9, 10, 11, 27, 28, 30, 31, 32, 37, 38, 39, 40, 50, 51, 55, 59,
    63, 64, 65, 66, 67, 68, 69, 70, 71, 72, 75, 76, 77, 78, 83, 85, 90, 93,
    94, 96, 104, 105, 106, 107, 108, 109, 111, 113, 114, 115, 117, 118, 119,
    120, 127, 131, 141, 142, 143
}

# These Java-mode overrides let us texture only a few familiar ground blocks
# from terrain.png while keeping every other imported Java block color-based.
JAVA_TERRAIN_TILES = {
    1: (0, 0),  # stone
    2: (1, 0),  # grass
    3: (2, 0),  # dirt
}

def block_coord(v):
    return int(math.floor(v))

def get_block_color(block_id):
    """Retourne la couleur d'un bloc ou gris par dÃ©faut"""
    return BLOCK_COLORS.get(block_id, (0.55, 0.55, 0.55))

def get_nibble(arr, index):
    """Lit un nibble (4 bits) depuis un tableau de bytes."""
    byte = arr[index >> 1]
    if index & 1:
        return (byte >> 4) & 0xF
    else:
        return byte & 0xF

def decode_chunk_data_1_8(data, primary_bitmask, add_bitmask, ground_up, overworld, chunk_x, chunk_z):
    """
    DÃ©code les donnÃ©es de chunk Minecraft 1.8 (protocole 47).

    Dans ce format, les block states sont stockÃ©s sur 2 octets par bloc
    (id << 4 | metadata), groupÃ©s pour toutes les sections d'abord,
    puis viennent les light arrays et enfin les biomes.
    """
    blocks = {}
    try:
        offset = 0
        present_sections = [
            section_y for section_y in range(16)
            if (primary_bitmask >> section_y) & 1
        ]
        section_payloads = {}

        for section_y in present_sections:
            section_payloads[section_y] = data[offset:offset+8192]
            offset += 8192

        offset += len(present_sections) * 2048  # block light
        if overworld:
            offset += len(present_sections) * 2048  # sky light

        add_arrays = {}
        for section_y in range(16):
            if not (add_bitmask >> section_y & 1):
                continue
            add_arrays[section_y] = data[offset:offset+2048]
            offset += 2048

        if ground_up:
            offset += 256  # biomes

        for section_y in present_sections:
            packed_blocks = section_payloads[section_y]
            add_arr = add_arrays.get(section_y)
            for i in range(4096):
                low = packed_blocks[i * 2]
                high = packed_blocks[i * 2 + 1]
                bid = (high << 4) | ((low >> 4) & 0x0F)
                if add_arr is not None:
                    bid |= get_nibble(add_arr, i) << 8
                if bid == 0:
                    continue

                bx_local = i & 0xF
                bz_local = (i >> 4) & 0xF
                by_local = i >> 8
                world_x = chunk_x * 16 + bx_local
                world_y = section_y * 16 + by_local
                world_z = chunk_z * 16 + bz_local
                blocks[(world_x, world_y, world_z)] = bid

    except Exception as e:
        print(f"[CHUNK DECODE ERROR] chunk ({chunk_x},{chunk_z}): {e}")

    return blocks

# =====================================================================
# AABB / LEVEL / PLAYER (inchangÃ©s + extension rÃ©seau Java)
# =====================================================================

class AABB:
    def __init__(self, x0, y0, z0, x1, y1, z1):
        self.x0, self.y0, self.z0 = x0, y0, z0
        self.x1, self.y1, self.z1 = x1, y1, z1
        self.eps = 0.01

    def clipX(self, c, xa):
        if c.y1 <= self.y0 or c.y0 >= self.y1 or c.z1 <= self.z0 or c.z0 >= self.z1: return xa
        if xa > 0 and c.x1 <= self.x0:
            v = self.x0 - c.x1 - self.eps
            if v < xa: xa = v
        if xa < 0 and c.x0 >= self.x1:
            v = self.x1 - c.x0 + self.eps
            if v > xa: xa = v
        return xa

    def clipY(self, c, ya):
        if c.x1 <= self.x0 or c.x0 >= self.x1 or c.z1 <= self.z0 or c.z0 >= self.z1: return ya
        if ya > 0 and c.y1 <= self.y0:
            v = self.y0 - c.y1 - self.eps
            if v < ya: ya = v
        if ya < 0 and c.y0 >= self.y1:
            v = self.y1 - c.y0 + self.eps
            if v > ya: ya = v
        return ya

    def clipZ(self, c, za):
        if c.x1 <= self.x0 or c.x0 >= self.x1 or c.y1 <= self.y0 or c.y0 >= self.y1: return za
        if za > 0 and c.z1 <= self.z0:
            v = self.z0 - c.z1 - self.eps
            if v < za: za = v
        if za < 0 and c.z0 >= self.z1:
            v = self.z1 - c.z0 + self.eps
            if v > za: za = v
        return za

    def move(self, xa, ya, za):
        self.x0 += xa; self.y0 += ya; self.z0 += za
        self.x1 += xa; self.y1 += ya; self.z1 += za

class Level:
    def __init__(self, w, d, h):
        self.w, self.d, self.h = w, d, h
        self.blocks = np.zeros((w, d, h), dtype=np.uint8)
        self.blocks[:, :30, :] = 1
        self.blocks[:, 30:32, :] = 2
        self.java_mode = False
        # Java mode keeps an unbounded sparse block store instead of the fixed solo/LAN array.
        self.java_blocks = {}  # (x, y, z) -> block_id
        self.java_chunk_blocks = {}  # (cx, cy, cz) -> {(x, y, z): block_id}
        self.java_visible_chunk_blocks = {}  # (cx, cy, cz) -> {(x, y, z): block_id}
        self.java_lock = threading.Lock()
        self.java_terrain_tex = None

    def is_solid(self, x, y, z):
        if self.java_mode:
            bid = self.java_blocks.get((block_coord(x), block_coord(y), block_coord(z)), 0)
            return bid > 0 and bid not in JAVA_NON_CUBE_BLOCKS
        if 0 <= x < self.w and 0 <= y < self.d and 0 <= z < self.h:
            return self.blocks[int(x), int(y), int(z)] > 0
        return False

    def get_block(self, x, y, z):
        if self.java_mode:
            return self.java_blocks.get((block_coord(x), block_coord(y), block_coord(z)), 0)
        if 0 <= x < self.w and 0 <= y < self.d and 0 <= z < self.h:
            return self.blocks[int(x), int(y), int(z)]
        return 0

    def get_java_chunk_key(self, x, y, z):
        return (
            (int(x) // CHUNK_SIZE_RENDER) * CHUNK_SIZE_RENDER,
            (int(y) // CHUNK_SIZE_RENDER) * CHUNK_SIZE_RENDER,
            (int(z) // CHUNK_SIZE_RENDER) * CHUNK_SIZE_RENDER,
        )

    def rebuild_java_visible_chunk(self, chunk_key):
        chunk_blocks = self.java_chunk_blocks.get(chunk_key, {})
        visible = {}
        for (bx, by, bz), bid in chunk_blocks.items():
            if bid == 0 or bid in JAVA_NON_CUBE_BLOCKS:
                continue
            if (not self.is_solid(bx + 1, by, bz) or
                not self.is_solid(bx - 1, by, bz) or
                not self.is_solid(bx, by + 1, bz) or
                not self.is_solid(bx, by - 1, bz) or
                not self.is_solid(bx, by, bz + 1) or
                not self.is_solid(bx, by, bz - 1)):
                visible[(bx, by, bz)] = bid
        if visible:
            self.java_visible_chunk_blocks[chunk_key] = visible
        else:
            self.java_visible_chunk_blocks.pop(chunk_key, None)

    def rebuild_all_java_chunk_maps(self):
        rebuilt = {}
        for pos, bid in self.java_blocks.items():
            if bid == 0:
                continue
            key = self.get_java_chunk_key(*pos)
            if key not in rebuilt:
                rebuilt[key] = {}
            rebuilt[key][pos] = bid
        self.java_chunk_blocks = rebuilt
        self.java_visible_chunk_blocks.clear()
        return set(rebuilt.keys())

class Player:
    def __init__(self, level):
        self.level = level
        self.x, self.y, self.z = 64.0, 35.0, 64.0
        self.xd, self.yd, self.zd = 0, 0, 0
        self.yRot, self.xRot = 0, 0
        self.bb = AABB(self.x-0.3, self.y-1.6, self.z-0.3, self.x+0.3, self.y+0.2, self.z+0.3)
        self.onGround = False

    def tick(self):
        keys = pygame.key.get_pressed()
        xa, za = 0, 0
        if keys[K_z] or keys[K_w]: za -= 1
        if keys[K_s]: za += 1
        if keys[K_q] or keys[K_a]: xa -= 1
        if keys[K_d]: xa += 1
        if keys[K_SPACE] and self.onGround: self.yd = 0.12
        speed = 0.04 if self.onGround else 0.02
        m = math.sqrt(xa*xa + za*za)
        if m > 0.01:
            xa *= speed/m; za *= speed/m
            s, c = math.sin(math.radians(self.yRot)), math.cos(math.radians(self.yRot))
            self.xd += xa * c - za * s
            self.zd += za * c + xa * s
        self.yd -= 0.005
        self.move(self.xd, self.yd, self.zd)
        self.xd *= 0.91; self.yd *= 0.98; self.zd *= 0.91
        if self.onGround: self.xd *= 0.7; self.zd *= 0.7

    def move(self, xa, ya, za):
        yO = ya
        cubes = []
        for ix in range(int(self.bb.x0-1), int(self.bb.x1+2)):
            for iy in range(int(self.bb.y0-1), int(self.bb.y1+2)):
                for iz in range(int(self.bb.z0-1), int(self.bb.z1+2)):
                    if self.level.is_solid(ix, iy, iz): cubes.append(AABB(ix,iy,iz,ix+1,iy+1,iz+1))
        for c in cubes: ya = c.clipY(self.bb, ya)
        self.bb.move(0, ya, 0)
        for c in cubes: xa = c.clipX(self.bb, xa)
        self.bb.move(xa, 0, 0)
        for c in cubes: za = c.clipZ(self.bb, za)
        self.bb.move(0, 0, za)
        self.onGround = (yO != ya and yO < 0)
        if yO != ya: self.yd = 0
        self.x, self.y, self.z = (self.bb.x0+self.bb.x1)/2, self.bb.y0+1.62, (self.bb.z0+self.bb.z1)/2

# =====================================================================
# CHUNK RENDERER - VERSION Ã‰TENDUE (couleur par block_id)
# =====================================================================

CHUNK_SIZE_RENDER = 16

class Chunk:
    def __init__(self, x0, y0, z0, level):
        self.pos = (x0, y0, z0)
        self.level = level
        self.list_id = glGenLists(1)
        self.dirty = True

    def _emit_java_face(self, textured, shade, vertices, uv):
        if textured:
            glColor3f(shade, shade, shade)
            glTexCoord2f(*uv[0]); glVertex3f(*vertices[0])
            glTexCoord2f(*uv[1]); glVertex3f(*vertices[1])
            glTexCoord2f(*uv[2]); glVertex3f(*vertices[2])
            glTexCoord2f(*uv[3]); glVertex3f(*vertices[3])
        else:
            glColor3f(*shade)
            for vx, vy, vz in vertices:
                glVertex3f(vx, vy, vz)

    def _emit_java_block(self, x, y, z, block_id, textured):
        r, g, bl = get_block_color(block_id)
        uv = None
        if textured:
            # The requested Java texture mapping uses the first three tiles of terrain.png:
            # stone, grass, dirt. Everything else still falls back to flat colors.
            tx, ty = JAVA_TERRAIN_TILES[block_id]
            u0 = tx / 16.0
            v0 = ty / 16.0
            u1 = (tx + 1) / 16.0
            v1 = (ty + 1) / 16.0
            uv = ((u0, v0), (u1, v0), (u1, v1), (u0, v1))

        if not self.level.is_solid(x, y+1, z):
            self._emit_java_face(
                textured, 1.0 if textured else (r, g, bl),
                ((x, y+1, z), (x, y+1, z+1), (x+1, y+1, z+1), (x+1, y+1, z)),
                uv
            )
        if not self.level.is_solid(x, y-1, z):
            self._emit_java_face(
                textured, 0.5 if textured else (r * 0.5, g * 0.5, bl * 0.5),
                ((x+1, y, z), (x+1, y, z+1), (x, y, z+1), (x, y, z)),
                uv
            )
        if not self.level.is_solid(x, y, z+1):
            self._emit_java_face(
                textured, 0.8 if textured else (r * 0.8, g * 0.8, bl * 0.8),
                ((x, y, z+1), (x+1, y, z+1), (x+1, y+1, z+1), (x, y+1, z+1)),
                uv
            )
        if not self.level.is_solid(x, y, z-1):
            self._emit_java_face(
                textured, 0.8 if textured else (r * 0.8, g * 0.8, bl * 0.8),
                ((x+1, y, z), (x, y, z), (x, y+1, z), (x+1, y+1, z)),
                uv
            )
        if not self.level.is_solid(x+1, y, z):
            self._emit_java_face(
                textured, 0.6 if textured else (r * 0.6, g * 0.6, bl * 0.6),
                ((x+1, y, z+1), (x+1, y, z), (x+1, y+1, z), (x+1, y+1, z+1)),
                uv
            )
        if not self.level.is_solid(x-1, y, z):
            self._emit_java_face(
                textured, 0.6 if textured else (r * 0.6, g * 0.6, bl * 0.6),
                ((x, y, z), (x, y, z+1), (x, y+1, z+1), (x, y+1, z)),
                uv
            )

    def _draw_geometry(self):
        x0, y0, z0 = self.pos

        t0 = (0.0, 0.0)
        t1 = (1.0/16.0, 0.0)
        t2 = (1.0/16.0, 1.0/16.0)
        t3 = (0.0, 1.0/16.0)

        if self.level.java_mode:
            with self.level.java_lock:
                chunk_blocks = self.level.java_visible_chunk_blocks.get((x0, y0, z0), {})
                candidates = [
                    (bx, by, bz, bid)
                    for ((bx, by, bz), bid) in list(chunk_blocks.items())
                    if bid != 0 and bid not in JAVA_NON_CUBE_BLOCKS
                ]

            # Java chunks are rendered in two passes:
            # 1. a textured pass for stone/grass/dirt from terrain.png
            # 2. a color pass for all remaining imported Java blocks
            textured_blocks = [item for item in candidates if item[3] in JAVA_TERRAIN_TILES]
            colored_blocks = [item for item in candidates if item[3] not in JAVA_TERRAIN_TILES]

            if textured_blocks and self.level.java_terrain_tex:
                glEnable(GL_TEXTURE_2D)
                glBindTexture(GL_TEXTURE_2D, self.level.java_terrain_tex)
                glBegin(GL_QUADS)
                for (x, y, z, b) in textured_blocks:
                    self._emit_java_block(x, y, z, b, textured=True)
                glEnd()

            glDisable(GL_TEXTURE_2D)
            glBegin(GL_QUADS)
            for (x, y, z, b) in colored_blocks:
                self._emit_java_block(x, y, z, b, textured=False)
            glEnd()

        else:
            glBegin(GL_QUADS)
            for x in range(x0, min(x0 + CHUNK_SIZE_RENDER, self.level.w)):
                for y in range(y0, min(y0 + CHUNK_SIZE_RENDER, self.level.d)):
                    for z in range(z0, min(z0 + CHUNK_SIZE_RENDER, self.level.h)):
                        b = self.level.blocks[x, y, z]
                        if b == 0:
                            continue
                        # Solo/LAN intentionally keeps the original atlas rules from the
                        # reference Nanocraft script so its visuals stay unchanged.
                        s = 0.0625
                        u = (b - 1) * s
                        v = 1.0 - s

                        # Top
                        if not self.level.is_solid(x, y+1, z):
                            glColor3f(1.0, 1.0, 1.0)
                            glTexCoord2f(u, v+s); glVertex3f(x, y+1, z)
                            glTexCoord2f(u, v); glVertex3f(x, y+1, z+1)
                            glTexCoord2f(u+s, v); glVertex3f(x+1, y+1, z+1)
                            glTexCoord2f(u+s, v+s); glVertex3f(x+1, y+1, z)

                        # Bottom
                        if not self.level.is_solid(x, y-1, z):
                            glColor3f(0.6, 0.6, 0.6)
                            glTexCoord2f(u+s, v+s); glVertex3f(x+1, y, z)
                            glTexCoord2f(u+s, v); glVertex3f(x+1, y, z+1)
                            glTexCoord2f(u, v); glVertex3f(x, y, z+1)
                            glTexCoord2f(u, v+s); glVertex3f(x, y, z)

                        # South
                        if not self.level.is_solid(x, y, z+1):
                            glColor3f(0.8, 0.8, 0.8)
                            glTexCoord2f(u, v); glVertex3f(x, y, z+1)
                            glTexCoord2f(u+s, v); glVertex3f(x+1, y, z+1)
                            glTexCoord2f(u+s, v+s); glVertex3f(x+1, y+1, z+1)
                            glTexCoord2f(u, v+s); glVertex3f(x, y+1, z+1)

                        # North
                        if not self.level.is_solid(x, y, z-1):
                            glColor3f(0.8, 0.8, 0.8)
                            glTexCoord2f(u+s, v); glVertex3f(x+1, y, z)
                            glTexCoord2f(u, v); glVertex3f(x, y, z)
                            glTexCoord2f(u, v+s); glVertex3f(x, y+1, z)
                            glTexCoord2f(u+s, v+s); glVertex3f(x+1, y+1, z)

                        # East
                        if not self.level.is_solid(x+1, y, z):
                            glColor3f(0.7, 0.7, 0.7)
                            glTexCoord2f(u+s, v); glVertex3f(x+1, y, z+1)
                            glTexCoord2f(u, v); glVertex3f(x+1, y, z)
                            glTexCoord2f(u, v+s); glVertex3f(x+1, y+1, z)
                            glTexCoord2f(u+s, v+s); glVertex3f(x+1, y+1, z+1)

                        # West
                        if not self.level.is_solid(x-1, y, z):
                            glColor3f(0.7, 0.7, 0.7)
                            glTexCoord2f(u, v); glVertex3f(x, y, z)
                            glTexCoord2f(u+s, v); glVertex3f(x, y, z+1)
                            glTexCoord2f(u+s, v+s); glVertex3f(x, y+1, z+1)
                            glTexCoord2f(u, v+s); glVertex3f(x, y+1, z)
            glEnd()

    def build(self):
        if self.list_id is None:
            self.list_id = glGenLists(1)
        if not self.list_id:
            raise RuntimeError("glGenLists a retournÃ© 0")

        if self.level.java_mode:
            with self.level.java_lock:
                self.level.rebuild_java_visible_chunk(self.pos)

        glNewList(self.list_id, GL_COMPILE)
        if self.level.java_mode:
            glDisable(GL_TEXTURE_2D)
        else:
            glEnable(GL_TEXTURE_2D)
        self._draw_geometry()
        if self.level.java_mode:
            glColor3f(1.0, 1.0, 1.0)
        else:
            glDisable(GL_TEXTURE_2D)
        glEndList()
        self.dirty = False

    def render_immediate(self):
        if self.level.java_mode:
            glDisable(GL_TEXTURE_2D)
        else:
            glEnable(GL_TEXTURE_2D)
        self._draw_geometry()
        if self.level.java_mode:
            glColor3f(1.0, 1.0, 1.0)
        else:
            glDisable(GL_TEXTURE_2D)

# =====================================================================
# CLIENT MINECRAFT JAVA 1.8
# =====================================================================

class MinecraftJavaClient:
    """
    Client minimal pour se connecter Ã  un serveur Minecraft 1.8 (protocole 47)
    Mode: OFFLINE (sans authentification Mojang)
    """
    
    PROTOCOL_VERSION = 47  # 1.8
    
    def __init__(self, host, port, username, level, player, on_status):
        self.host = host
        self.port = port
        self.username = username
        self.level = level
        self.player = player
        self.on_status = on_status  # callback(str)
        self.sock = None
        self.running = False
        self.compression_threshold = -1
        self.state = "handshake"
        self.pending_chunks = []
        self.chunk_lock = self.level.java_lock
        self.connected = False
        self.dirty_chunks = set()  # chunk positions (cx, cy, cz) Ã  rebuilder
        self.entity_lock = threading.Lock()
        self.remote_players = {}   # entity_id -> state dict
        self.entity_id = None
        self.gamemode = 0
        self.held_slot = 0
        self.chunks_received = 0
        self.last_chunk_time = 0.0
        self._thread = None
        self.chunk_executor = ThreadPoolExecutor(max_workers=JAVA_CHUNK_WORKERS)
        self.decode_lock = threading.Lock()
        self.chunk_decode_seq = 0
        self.chunk_apply_seq = 0
        self.pending_chunk_futures = set()
        self.completed_chunk_results = {}

    def connect(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        try:
            self.on_status("Connecting to {}:{}...".format(self.host, self.port))
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(10)
            self.sock.connect((self.host, self.port))
            self.sock.settimeout(None)
            self.on_status("Connected! Handshake...")
            
            # 1. Handshake
            self._send_handshake()
            
            # 2. Login Request
            self._send_login_start()
            
            # 3. Attendre Login Success ou Enable Compression
            self._login_loop()
            
        except Exception as e:
            self.on_status("Error: {}".format(str(e)))
            self.connected = False
            return

    def _send(self, packet_id, payload):
        """Envoie un paquet en tenant compte du threshold de compression courant."""
        self.sock.sendall(write_packet(packet_id, payload, self.compression_threshold))

    def _send_handshake(self):
        """Paquet 0x00 Handshake"""
        payload = (
            write_varint(self.PROTOCOL_VERSION) +
            write_string(self.host) +
            struct.pack('>H', self.port) +
            write_varint(2)
        )
        # Handshake avant login : pas encore de compression
        self.sock.sendall(write_packet(0x00, payload, -1))
        self.state = "login"

    def _send_login_start(self):
        """Paquet 0x00 Login Start"""
        payload = write_string(self.username)
        # Login start : compression pas encore activÃ©e
        self.sock.sendall(write_packet(0x00, payload, -1))

    def _login_loop(self):
        """GÃ¨re les paquets de login jusqu'Ã  entrer en jeu"""
        while True:
            pid, data, off = recv_packet(self.sock, self.compression_threshold)
            
            if self.state == "login":
                if pid == 0x03:
                    # Set Compression
                    threshold, _ = read_varint(data, off)
                    self.compression_threshold = threshold
                    self.on_status("Compression enabled (threshold={})".format(threshold))
                    
                elif pid == 0x02:
                    # Login Success
                    uuid, off = read_string(data, off)
                    name, off = read_string(data, off)
                    self.on_status("Login OK: {} ({})".format(name, uuid[:8]))
                    self.state = "play"
                    self._play_loop()
                    return
                    
                elif pid == 0x01:
                    # Encryption Request - serveur en mode online, on ne supporte pas
                    self.on_status("ERROR: Server is in online mode. Set online-mode=false in server.properties")
                    return
                    
                elif pid == 0x00:
                    # Disconnect
                    reason, _ = read_string(data, off)
                    self.on_status("Rejected: {}".format(reason[:80]))
                    return

    def _play_loop(self):
        """Boucle principale de jeu - reÃ§oit et traite les paquets"""
        self.connected = True
        self.on_status("In game! Loading chunks...")
        self.level.java_mode = True
        
        last_pos_send = time.time()
        self.sock.settimeout(0.05)
        self.spawned = False   # on n'envoie pas de position avant le 0x08 spawn
        chunks_received = 0
        
        while self.connected:
            try:
                pid, data, off = recv_packet(self.sock, self.compression_threshold)
                print(f"[PKT] 0x{pid:02X} len={len(data)}")

                # 0x01 - Join Game (1.8 protocol 47)
                # entity_id(int) + gamemode(ubyte) + dimension(byte) + difficulty(ubyte)
                # + max_players(ubyte) + level_type(string) + reduced_debug(bool)
                if pid == 0x01:
                    entity_id = struct.unpack_from('>i', data, off)[0]; off += 4
                    self.entity_id = entity_id
                    gamemode   = data[off]; off += 1
                    self.gamemode = gamemode & 0x7
                    dimension  = struct.unpack_from('>b', data, off)[0]; off += 1  # signed byte!
                    difficulty = data[off]; off += 1
                    max_players= data[off]; off += 1
                    level_type, off = read_string(data, off)
                    reduced_debug = data[off]; off += 1
                    self.on_status("Joined world (dim={} mode={})".format(dimension, gamemode))
                    # RÃ©pondre avec ClientSettings pour activer le spawn et la rÃ©ception des chunks
                    self._send_client_settings()
                
                # 0x08 - Player Position and Look (serveur -> client, force position)
                # En 1.8 le serveur envoie les eyes_y (y + 1.62) quand il teleporte.
                # En rÃ©alitÃ© il envoie feet_y. On stocke eyes_y = feet_y + 1.62.
                elif pid == 0x08:
                    feet_x = struct.unpack_from('>d', data, off)[0]; off += 8
                    feet_y = struct.unpack_from('>d', data, off)[0]; off += 8
                    feet_z = struct.unpack_from('>d', data, off)[0]; off += 8
                    yaw    = struct.unpack_from('>f', data, off)[0]; off += 4
                    pitch  = struct.unpack_from('>f', data, off)[0]; off += 4
                    flags  = data[off]; off += 1
                    # Flags: bits indiquent si relatif ou absolu (1.8 = toujours absolu)
                    eyes_y = feet_y + 1.62
                    self.player.x    = feet_x
                    self.player.y    = eyes_y
                    self.player.z    = feet_z
                    self.player.yRot = yaw
                    self.player.xRot = pitch
                    self.player.bb   = AABB(feet_x-0.3, feet_y, feet_z-0.3, feet_x+0.3, feet_y+1.8, feet_z+0.3)
                    # Renvoyer la position exacte pour confirmer le tÃ©lÃ©port
                    self._send_position(feet_x, eyes_y, feet_z, yaw, pitch)
                    self.spawned = True
                    self.on_status("Spawn: ({:.1f}, {:.1f}, {:.1f})".format(feet_x, feet_y, feet_z))
                
                # 0x21 - Chunk Data single (rare en 1.8, surtout utilisÃ© pour effacer)
                elif pid == 0x0C:
                    self._handle_spawn_player(data, off)

                elif pid == 0x13:
                    self._handle_destroy_entities(data, off)

                elif pid == 0x15:
                    self._handle_entity_relative_move(data, off, update_rotation=False)

                elif pid == 0x16:
                    self._handle_entity_look(data, off)

                elif pid == 0x17:
                    self._handle_entity_relative_move(data, off, update_rotation=True)

                elif pid == 0x18:
                    self._handle_entity_teleport(data, off)

                elif pid == 0x19:
                    self._handle_entity_head_look(data, off)

                elif pid == 0x21:
                    self._handle_chunk_single(data, off)
                    chunks_received += 1
                    self.chunks_received = chunks_received
                    self.last_chunk_time = time.time()
                    if chunks_received % 10 == 0:
                        self.on_status("Chunks received: {}".format(chunks_received))

                # 0x26 - Map Chunk Bulk (envoi groupÃ© de plusieurs chunks, format principal 1.8)
                elif pid == 0x26:
                    n = self._handle_chunk_bulk(data, off)
                    chunks_received += n
                    self.chunks_received = chunks_received
                    self.last_chunk_time = time.time()
                    if chunks_received % 5 == 0:
                        self.on_status("Chunks received: {}".format(chunks_received))

                # 0x06 - Update Health (Ã©viter la mort : rÃ©pondre avec Respawn)
                elif pid == 0x06:
                    try:
                        # Lire les donnÃ©es correctement (1.8)
                        health = struct.unpack_from(">f", data, off)[0]; off += 4
                        food, off = read_varint(data, off)   # VarInt (important)
                        satur  = struct.unpack_from(">f", data, off)[0]; off += 4

                        print(f"[HEALTH] hp={health:.1f} food={food} sat={satur:.2f}")

                        # Si mort â†’ demander respawn (Client Status)
                        if health <= 0:
                            payload = write_varint(0)  # action_id = 0 (respawn)
                            self._send(0x16, payload)
                            print("[HEALTH] Respawn demandÃ©")

                    except Exception as e:
                        print(f"[HEALTH ERROR] {e}")
                        
                # 0x22 - Multi Block Change
                elif pid == 0x22:
                    self._handle_multi_block_change(data, off)
                
                # 0x23 - Block Change
                elif pid == 0x23:
                    self._handle_block_change(data, off)
                
                # 0x00 - Keep Alive (1.8)
                elif pid == 0x00:
                    # Renvoyer le keep alive
                    ka_id = struct.unpack_from('>i', data, off)[0]
                    payload = struct.pack('>i', ka_id)
                    self._send(0x00, payload)
                
                # 0x40 - Disconnect
                elif pid == 0x40:
                    reason, _ = read_string(data, off)
                    self.on_status("Disconnected: {}".format(reason[:60]))
                    self.connected = False
                    return
                    
            except socket.timeout:
                pass
            except Exception as e:
                if self.connected:
                    import traceback
                    print(f"[PLAY_LOOP ERROR] pid=0x{pid if 'pid' in dir() else '??':02X}: {e}")
                    traceback.print_exc()
                    # Ne pas quitter sur une erreur de parsing - juste logger et continuer
                return

            self._drain_decoded_chunks(32)
            
            # Envoyer position pÃ©riodiquement (seulement aprÃ¨s le spawn)
            now = time.time()
            if self.spawned and now - last_pos_send > 0.1:
                self._send_position(
                    self.player.x, self.player.y, self.player.z,
                    self.player.yRot, self.player.xRot
                )
                last_pos_send = now

    def _handle_chunk_single(self, data, off):
        """Paquet 0x21 Chunk Data (chunk unique, surtout utilisÃ© pour effacer en 1.8)."""
        try:
            chunk_x     = struct.unpack_from('>i', data, off)[0]; off += 4
            chunk_z     = struct.unpack_from('>i', data, off)[0]; off += 4
            ground_up   = bool(data[off]); off += 1
            primary_bitmask = struct.unpack_from('>H', data, off)[0]; off += 2
            data_size, off = read_varint(data, off)
            chunk_data  = bytes(data[off:off+data_size])
            self._schedule_chunk_decode(chunk_data, chunk_x, chunk_z, primary_bitmask, 0, ground_up)
        except Exception as e:
            print(f"[CHUNK_SINGLE ERROR] {e}")

    def _handle_spawn_player(self, data, off):
        try:
            entity_id, off = read_varint(data, off)
            if entity_id == self.entity_id:
                return

            uuid_bytes = bytes(data[off:off+16]); off += 16
            x = struct.unpack_from('>i', data, off)[0] / 32.0; off += 4
            y = struct.unpack_from('>i', data, off)[0] / 32.0; off += 4
            z = struct.unpack_from('>i', data, off)[0] / 32.0; off += 4
            yaw, off = read_angle(data, off)
            pitch, off = read_angle(data, off)
            current_item = struct.unpack_from('>h', data, off)[0]; off += 2

            with self.entity_lock:
                self.remote_players[entity_id] = {
                    "x": x,
                    "y": y + 1.62,
                    "z": z,
                    "yaw": yaw,
                    "pitch": pitch,
                    "head_yaw": yaw,
                    "uuid": uuid_bytes.hex(),
                    "item": current_item,
                }
        except Exception as e:
            print(f"[PLAYER SPAWN ERROR] {e}")

    def _handle_destroy_entities(self, data, off):
        try:
            count, off = read_varint(data, off)
            with self.entity_lock:
                for _ in range(count):
                    entity_id, off = read_varint(data, off)
                    self.remote_players.pop(entity_id, None)
        except Exception as e:
            print(f"[DESTROY ENTITIES ERROR] {e}")

    def _handle_entity_relative_move(self, data, off, update_rotation=False):
        try:
            entity_id, off = read_varint(data, off)
            dx = struct.unpack_from('>b', data, off)[0] / 32.0; off += 1
            dy = struct.unpack_from('>b', data, off)[0] / 32.0; off += 1
            dz = struct.unpack_from('>b', data, off)[0] / 32.0; off += 1

            yaw = pitch = None
            if update_rotation:
                yaw, off = read_angle(data, off)
                pitch, off = read_angle(data, off)

            if off < len(data):
                off += 1  # on_ground

            with self.entity_lock:
                player = self.remote_players.get(entity_id)
                if not player:
                    return
                player["x"] += dx
                player["y"] += dy
                player["z"] += dz
                if yaw is not None:
                    player["yaw"] = yaw
                    player["pitch"] = pitch
        except Exception as e:
            print(f"[ENTITY MOVE ERROR] {e}")

    def _handle_entity_look(self, data, off):
        try:
            entity_id, off = read_varint(data, off)
            yaw, off = read_angle(data, off)
            pitch, off = read_angle(data, off)
            if off < len(data):
                off += 1  # on_ground

            with self.entity_lock:
                player = self.remote_players.get(entity_id)
                if not player:
                    return
                player["yaw"] = yaw
                player["pitch"] = pitch
        except Exception as e:
            print(f"[ENTITY LOOK ERROR] {e}")

    def _handle_entity_teleport(self, data, off):
        try:
            entity_id, off = read_varint(data, off)
            x = struct.unpack_from('>i', data, off)[0] / 32.0; off += 4
            y = struct.unpack_from('>i', data, off)[0] / 32.0; off += 4
            z = struct.unpack_from('>i', data, off)[0] / 32.0; off += 4
            yaw, off = read_angle(data, off)
            pitch, off = read_angle(data, off)
            if off < len(data):
                off += 1  # on_ground

            with self.entity_lock:
                player = self.remote_players.get(entity_id)
                if not player:
                    return
                player["x"] = x
                player["y"] = y + 1.62
                player["z"] = z
                player["yaw"] = yaw
                player["pitch"] = pitch
        except Exception as e:
            print(f"[ENTITY TELEPORT ERROR] {e}")

    def _handle_entity_head_look(self, data, off):
        try:
            entity_id, off = read_varint(data, off)
            head_yaw, off = read_angle(data, off)

            with self.entity_lock:
                player = self.remote_players.get(entity_id)
                if not player:
                    return
                player["head_yaw"] = head_yaw
                player["yaw"] = head_yaw
        except Exception as e:
            print(f"[ENTITY HEAD LOOK ERROR] {e}")

    def _handle_chunk_bulk(self, data, off):
        """
        Paquet 0x26 Map Chunk Bulk (1.8).
        Layout principal observÃ© sur vanilla/spigot:
          bool   : sky light sent
          varint : nombre de chunks
          [int x, int z, ushort primary_bitmask] * N
          puis les bytes de chunks concatÃ©nÃ©s (pas de zlib interne ici,
          la compression packet-level est dÃ©jÃ gÃ©rÃ©e par recv_packet()).
        """
        try:
            body = bytes(data[off:])

            def chunk_payload_size(pbm, sky_light):
                n_primary = bin(pbm).count('1')
                size = n_primary * (8192 + 2048)
                if sky_light:
                    size += n_primary * 2048
                size += 256
                return size

            def try_layout_protocol47():
                cursor = 0
                sky_light = bool(body[cursor]); cursor += 1
                n_chunks, cursor = read_varint(body, cursor)
                metas = []
                for _ in range(n_chunks):
                    cx = struct.unpack_from('>i', body, cursor)[0]; cursor += 4
                    cz = struct.unpack_from('>i', body, cursor)[0]; cursor += 4
                    pbm = struct.unpack_from('>H', body, cursor)[0]; cursor += 2
                    metas.append((cx, cz, pbm))
                raw = body[cursor:]
                expected = sum(chunk_payload_size(pbm, sky_light) for (_, _, pbm) in metas)
                if len(raw) < expected:
                    raise ValueError(f"bulk raw too short ({len(raw)} < {expected})")
                return metas, raw, sky_light

            last_error = None
            for parser in (try_layout_protocol47,):
                try:
                    metas, raw, sky_light = parser()
                    break
                except Exception as e:
                    last_error = e
            else:
                raise last_error if last_error else ValueError("unknown bulk packet layout")

            raw_off = 0
            for (cx, cz, pbm) in metas:
                sec_size = chunk_payload_size(pbm, sky_light)
                chunk_raw = raw[raw_off:raw_off+sec_size]
                raw_off += sec_size
                self._schedule_chunk_decode(chunk_raw, cx, cz, pbm, 0, ground_up=True, sky_light=sky_light)

            print(f"[BULK] {len(metas)} chunks traites, blocs total: {len(self.level.java_blocks)}")
            return len(metas)
        except Exception as e:
            import traceback; traceback.print_exc()
            print(f"[CHUNK_BULK ERROR] {e}")
            return 0

    def _decode_chunk_task(self, seq, chunk_data, chunk_x, chunk_z, primary_bitmask, add_bitmask, ground_up, sky_light):
        try:
            new_blocks = decode_chunk_data_1_8(
                chunk_data, primary_bitmask, add_bitmask,
                ground_up, sky_light, chunk_x, chunk_z
            )
            section_ys = [
                section_y for section_y in range(16)
                if (primary_bitmask >> section_y) & 1
            ]
            return (seq, chunk_x, chunk_z, ground_up, section_ys, new_blocks, None)
        except Exception as e:
            return (seq, chunk_x, chunk_z, ground_up, None, None, str(e))

    def _store_chunk_result(self, result):
        seq = result[0]
        with self.decode_lock:
            self.completed_chunk_results[seq] = result

    def _schedule_chunk_decode(self, chunk_data, chunk_x, chunk_z, primary_bitmask, add_bitmask, ground_up, sky_light=True):
        with self.decode_lock:
            seq = self.chunk_decode_seq
            self.chunk_decode_seq += 1
            queued = len(self.pending_chunk_futures)

        if queued >= JAVA_CHUNK_QUEUE_LIMIT:
            self._store_chunk_result(
                self._decode_chunk_task(seq, chunk_data, chunk_x, chunk_z, primary_bitmask, add_bitmask, ground_up, sky_light)
            )
            return

        future = self.chunk_executor.submit(
            self._decode_chunk_task,
            seq, chunk_data, chunk_x, chunk_z, primary_bitmask, add_bitmask, ground_up, sky_light
        )
        with self.decode_lock:
            self.pending_chunk_futures.add(future)

        def _done_callback(done_future):
            with self.decode_lock:
                self.pending_chunk_futures.discard(done_future)
            try:
                result = done_future.result()
            except Exception as e:
                result = (seq, chunk_x, chunk_z, ground_up, None, None, str(e))
            self._store_chunk_result(result)

        future.add_done_callback(_done_callback)

    def _apply_decoded_chunk(self, chunk_x, chunk_z, ground_up, section_ys, new_blocks):
        with self.chunk_lock:
            affected = set()
            if ground_up:
                to_del = [k for k in self.level.java_blocks
                          if k[0] // 16 == chunk_x and k[2] // 16 == chunk_z]
                for k in to_del:
                    del self.level.java_blocks[k]
                chunk_keys_to_del = [
                    key for key in self.level.java_chunk_blocks
                    if key[0] // 16 == chunk_x and key[2] // 16 == chunk_z
                ]
                for key in chunk_keys_to_del:
                    del self.level.java_chunk_blocks[key]
                    self.level.java_visible_chunk_blocks.pop(key, None)
                affected.update(chunk_keys_to_del)
            elif section_ys:
                sections = set(section_ys)
                to_del = [
                    k for k in self.level.java_blocks
                    if k[0] // 16 == chunk_x and k[2] // 16 == chunk_z and (k[1] // 16) in sections
                ]
                for k in to_del:
                    del self.level.java_blocks[k]
                chunk_keys_to_rebuild = set()
                for key, bucket in list(self.level.java_chunk_blocks.items()):
                    if key[0] // 16 != chunk_x or key[2] // 16 != chunk_z:
                        continue
                    kept = {
                        pos: bid for pos, bid in bucket.items()
                        if (pos[1] // 16) not in sections
                    }
                    if kept:
                        self.level.java_chunk_blocks[key] = kept
                    else:
                        del self.level.java_chunk_blocks[key]
                    self.level.java_visible_chunk_blocks.pop(key, None)
                    chunk_keys_to_rebuild.add(key)
                affected.update(chunk_keys_to_rebuild)
            self.level.java_blocks.update(new_blocks)
            for pos, block_id in new_blocks.items():
                key = self.level.get_java_chunk_key(*pos)
                if key not in self.level.java_chunk_blocks:
                    self.level.java_chunk_blocks[key] = {}
                self.level.java_chunk_blocks[key][pos] = block_id
            for (bx, by, bz) in new_blocks:
                affected.add(((bx//16)*16, (by//16)*16, (bz//16)*16))

            expanded = set(affected)
            for (cx, cy, cz) in list(affected):
                expanded.add((cx + CHUNK_SIZE_RENDER, cy, cz))
                expanded.add((cx - CHUNK_SIZE_RENDER, cy, cz))
                expanded.add((cx, cy + CHUNK_SIZE_RENDER, cz))
                expanded.add((cx, cy - CHUNK_SIZE_RENDER, cz))
                expanded.add((cx, cy, cz + CHUNK_SIZE_RENDER))
                expanded.add((cx, cy, cz - CHUNK_SIZE_RENDER))
            self.dirty_chunks.update(expanded)

    def _drain_decoded_chunks(self, max_chunks=None):
        applied = 0
        while True:
            with self.decode_lock:
                result = self.completed_chunk_results.pop(self.chunk_apply_seq, None)
            if result is None:
                break

            _, chunk_x, chunk_z, ground_up, section_ys, new_blocks, error = result
            if error:
                print(f"[APPLY_CHUNK ERROR] ({chunk_x},{chunk_z}): {error}")
            else:
                self._apply_decoded_chunk(chunk_x, chunk_z, ground_up, section_ys, new_blocks)

            self.chunk_apply_seq += 1
            applied += 1
            if max_chunks is not None and applied >= max_chunks:
                break
        return applied

    def _mark_dirty_block_chunks(self, bx, by, bz):
        cx = (bx // CHUNK_SIZE_RENDER) * CHUNK_SIZE_RENDER
        cy = (by // CHUNK_SIZE_RENDER) * CHUNK_SIZE_RENDER
        cz = (bz // CHUNK_SIZE_RENDER) * CHUNK_SIZE_RENDER
        self.dirty_chunks.add((cx, cy, cz))

        local_x = bx - cx
        local_y = by - cy
        local_z = bz - cz
        if local_x == 0:
            self.dirty_chunks.add((cx - CHUNK_SIZE_RENDER, cy, cz))
        elif local_x == CHUNK_SIZE_RENDER - 1:
            self.dirty_chunks.add((cx + CHUNK_SIZE_RENDER, cy, cz))
        if local_y == 0:
            self.dirty_chunks.add((cx, cy - CHUNK_SIZE_RENDER, cz))
        elif local_y == CHUNK_SIZE_RENDER - 1:
            self.dirty_chunks.add((cx, cy + CHUNK_SIZE_RENDER, cz))
        if local_z == 0:
            self.dirty_chunks.add((cx, cy, cz - CHUNK_SIZE_RENDER))
        elif local_z == CHUNK_SIZE_RENDER - 1:
            self.dirty_chunks.add((cx, cy, cz + CHUNK_SIZE_RENDER))

    def _handle_block_change(self, data, off):
        """Paquet 0x23 - Block Change"""
        try:
            # Position encodÃ©e en long (1.8 block position format)
            pos_long = struct.unpack_from('>q', data, off)[0]; off += 8
            bx = pos_long >> 38
            by = (pos_long >> 26) & 0xFFF
            bz = pos_long << 38 >> 38
            # Signe
            if bx >= (1 << 25): bx -= (1 << 26)
            if bz >= (1 << 25): bz -= (1 << 26)
            
            block_id_raw, off = read_varint(data, off)
            block_id = block_id_raw >> 4
            
            with self.chunk_lock:
                if block_id == 0:
                    self.level.java_blocks.pop((bx, by, bz), None)
                    key = self.level.get_java_chunk_key(bx, by, bz)
                    bucket = self.level.java_chunk_blocks.get(key)
                    if bucket is not None:
                        bucket.pop((bx, by, bz), None)
                        if not bucket:
                            del self.level.java_chunk_blocks[key]
                            self.level.java_visible_chunk_blocks.pop(key, None)
                else:
                    self.level.java_blocks[(bx, by, bz)] = block_id
                    key = self.level.get_java_chunk_key(bx, by, bz)
                    if key not in self.level.java_chunk_blocks:
                        self.level.java_chunk_blocks[key] = {}
                    self.level.java_chunk_blocks[key][(bx, by, bz)] = block_id

                self._mark_dirty_block_chunks(bx, by, bz)
        except:
            pass

    def _handle_multi_block_change(self, data, off):
        """Paquet 0x22 - Multi Block Change"""
        try:
            chunk_x = struct.unpack_from('>i', data, off)[0]; off += 4
            chunk_z = struct.unpack_from('>i', data, off)[0]; off += 4
            record_count = struct.unpack_from('>H', data, off)[0]; off += 2
            
            for _ in range(record_count):
                horiz = data[off]; off += 1
                y = data[off]; off += 1
                new_block, o2 = read_varint(data, off)
                off = o2
                
                bx = chunk_x * 16 + (horiz >> 4)
                bz = chunk_z * 16 + (horiz & 0xF)
                block_id = new_block >> 4
                
                with self.chunk_lock:
                    if block_id == 0:
                        self.level.java_blocks.pop((bx, y, bz), None)
                        key = self.level.get_java_chunk_key(bx, y, bz)
                        bucket = self.level.java_chunk_blocks.get(key)
                        if bucket is not None:
                            bucket.pop((bx, y, bz), None)
                            if not bucket:
                                del self.level.java_chunk_blocks[key]
                                self.level.java_visible_chunk_blocks.pop(key, None)
                    else:
                        self.level.java_blocks[(bx, y, bz)] = block_id
                        key = self.level.get_java_chunk_key(bx, y, bz)
                        if key not in self.level.java_chunk_blocks:
                            self.level.java_chunk_blocks[key] = {}
                        self.level.java_chunk_blocks[key][(bx, y, bz)] = block_id

                    self._mark_dirty_block_chunks(bx, y, bz)
        except:
            pass

    def _send_position(self, x, y, z, yaw, pitch):
        """Envoie Player Position And Look (0x06).
        player.y = eyes_y, le serveur attend feet_y = eyes_y - 1.62.
        on_ground=False pour Ã©viter le ban anti-cheat "flying".
        """
        try:
            feet_y = y - 1.62
            server_yaw = (yaw + 180.0) % 360.0
            payload = (
                struct.pack('>d', x) +
                struct.pack('>d', feet_y) +
                struct.pack('>d', z) +
                struct.pack('>f', server_yaw) +
                struct.pack('>f', pitch) +
                struct.pack('>?', False)   # on_ground=False, Ã©vite "flying not enabled"
            )
            self._send(0x06, payload)
        except:
            pass

    def _send_client_settings(self):
        try:
            payload = (
                write_string("fr_FR") +        # locale
                struct.pack(">b", 10) +        # view distance
                write_varint(0) +              # chat mode (VarInt !)
                struct.pack(">?", True) +      # chat colors
                struct.pack(">B", 127)         # skin parts (unsigned byte)
            )
            self._send(0x15, payload)
            print("[CLIENT] Client Settings envoyÃ© (1.8 OK)")
        except Exception as e:
            print(f"[CLIENT] Erreur client settings: {e}")

    def send_held_item_change(self, slot):
        try:
            self.held_slot = max(0, min(8, int(slot)))
            self._send(0x09, struct.pack(">h", self.held_slot))
        except Exception as e:
            print(f"[CLIENT] Erreur held item change: {e}")

    def send_creative_inventory_action(self, slot, item_id, count=1, damage=0):
        try:
            payload = struct.pack(">h", slot) + write_slot(item_id, count, damage)
            self._send(0x10, payload)
            return True
        except Exception as e:
            print(f"[CLIENT] Erreur creative inventory action: {e}")
            return False

    def send_dig_status(self, status, pos, face):
        try:
            payload = struct.pack(">b", status) + pack_block_position(*pos) + struct.pack(">B", face)
            self._send(0x07, payload)
            return True
        except Exception as e:
            print(f"[CLIENT] Erreur dig block: {e}")
            return False

    def send_dig_block(self, pos, face):
        if not self.send_dig_status(0, pos, face):
            return False
        return self.send_dig_status(2, pos, face)

    def send_place_block(self, target_pos, face, item_id):
        try:
            if self.gamemode == 1:
                self.send_creative_inventory_action(36 + self.held_slot, item_id, 64, 0)
            payload = (
                pack_block_position(*target_pos) +
                struct.pack(">B", face) +
                write_slot(item_id, 1, 0) +
                struct.pack(">BBB", 8, 8, 8)
            )
            self._send(0x08, payload)
            return True
        except Exception as e:
            print(f"[CLIENT] Erreur place block: {e}")
            return False

    def disconnect(self):
        self.connected = False
        self._drain_decoded_chunks()
        with self.entity_lock:
            self.remote_players.clear()
        if self.chunk_executor:
            try:
                self.chunk_executor.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                self.chunk_executor.shutdown(wait=False)
        if self.sock:
            try: self.sock.close()
            except: pass

# =====================================================================
# SERVEUR JAVA LAN MINIMAL (1.8 / protocole 47)
# =====================================================================

class JavaLanServer:
    PROTOCOL_VERSION = 47

    def __init__(self, level, on_status=None, on_block_update=None):
        self.level = level
        self.on_status = on_status
        self.on_block_update = on_block_update
        self.running = False
        self.sock = None
        self.port = None
        self.clients = []
        self.clients_lock = threading.Lock()
        self.entity_seq = 1000
        self.accept_thread = None
        self.announce_thread = None

    def start(self, preferred_port=JAVA_LAN_PORT_DEFAULT):
        if self.running:
            return self.port

        bind_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        bind_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        chosen_port = None
        for port in range(preferred_port, preferred_port + 20):
            try:
                bind_sock.bind(("0.0.0.0", port))
                chosen_port = port
                break
            except OSError:
                continue
        if chosen_port is None:
            bind_sock.close()
            raise OSError("Impossible de trouver un port Java LAN libre")

        bind_sock.listen(5)
        bind_sock.settimeout(0.5)
        self.sock = bind_sock
        self.port = chosen_port
        self.running = True
        self.accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self.accept_thread.start()
        self.announce_thread = threading.Thread(target=self._announce_loop, daemon=True)
        self.announce_thread.start()
        self._status(f"Java LAN server on port {self.port}")
        return self.port

    def stop(self):
        self.running = False
        if self.sock:
            try:
                self.sock.close()
            except:
                pass
            self.sock = None
        with self.clients_lock:
            clients = list(self.clients)
            self.clients.clear()
        for client in clients:
            try:
                client["sock"].close()
            except:
                pass

    def _status(self, msg):
        if self.on_status:
            self.on_status(msg)
        print("[JAVA LAN]", msg)

    def _send_packet(self, sock, packet_id, payload):
        sock.sendall(write_packet(packet_id, payload, -1))

    def _accept_loop(self):
        while self.running and self.sock:
            try:
                client_sock, addr = self.sock.accept()
                client_sock.settimeout(10)
                threading.Thread(target=self._handle_client, args=(client_sock, addr), daemon=True).start()
            except socket.timeout:
                continue
            except OSError:
                break
            except Exception as e:
                self._status(f"Accept error: {e}")

    def _announce_loop(self):
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            udp.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
            while self.running:
                motd = f"[MOTD]Nanocraft LAN[/MOTD][AD]{self.port}[/AD]"
                try:
                    udp.sendto(motd.encode("utf-8"), (JAVA_LAN_MULTICAST_GROUP, JAVA_LAN_MULTICAST_PORT))
                except:
                    pass
                time.sleep(1.5)
        finally:
            udp.close()

    def _handle_client(self, client_sock, addr):
        try:
            pid, data, off = recv_packet(client_sock, -1)
            if pid != 0x00:
                client_sock.close()
                return

            protocol, off = read_varint(data, off)
            host, off = read_string(data, off)
            server_port = struct.unpack_from(">H", data, off)[0]; off += 2
            next_state, off = read_varint(data, off)

            if next_state == 1:
                self._handle_status(client_sock)
                return
            if next_state == 2:
                self._handle_login(client_sock)
                return
        except Exception as e:
            self._status(f"Client error {addr}: {e}")
        try:
            client_sock.close()
        except:
            pass

    def _handle_status(self, client_sock):
        pid, data, off = recv_packet(client_sock, -1)
        if pid == 0x00:
            status = {
                "version": {"name": "1.8", "protocol": self.PROTOCOL_VERSION},
                "players": {"max": 8, "online": 0, "sample": []},
                "description": {"text": "Nanocraft LAN"},
            }
            self._send_packet(client_sock, 0x00, write_string(json.dumps(status)))
        pid, data, off = recv_packet(client_sock, -1)
        if pid == 0x01:
            self._send_packet(client_sock, 0x01, data[off:])
        client_sock.close()

    def _handle_login(self, client_sock):
        pid, data, off = recv_packet(client_sock, -1)
        if pid != 0x00:
            client_sock.close()
            return
        username, off = read_string(data, off)
        user_uuid = str(uuid.uuid3(uuid.NAMESPACE_DNS, "nanocraft:" + username))
        self._send_packet(client_sock, 0x02, write_string(user_uuid) + write_string(username))
        self._enter_play(client_sock, username)

    def _apply_block_update(self, pos, block_id):
        if self.on_block_update:
            self.on_block_update(pos, block_id, True)
            return
        x, y, z = pos
        if 0 <= x < self.level.w and 0 <= y < self.level.d and 0 <= z < self.level.h:
            self.level.blocks[x, y, z] = block_id

    def _offset_from_face(self, face):
        if face == 0:
            return (0, -1, 0)
        if face == 1:
            return (0, 1, 0)
        if face == 2:
            return (0, 0, -1)
        if face == 3:
            return (0, 0, 1)
        if face == 4:
            return (-1, 0, 0)
        if face == 5:
            return (1, 0, 0)
        return (0, 0, 0)

    def _handle_play_packet(self, pid, data, off):
        try:
            if pid == 0x07:
                status = struct.unpack_from(">b", data, off)[0]; off += 1
                pos_long = struct.unpack_from(">q", data, off)[0]; off += 8
                face = data[off]
                bx = pos_long >> 38
                by = (pos_long >> 26) & 0xFFF
                bz = pos_long << 38 >> 38
                if bx >= (1 << 25): bx -= (1 << 26)
                if bz >= (1 << 25): bz -= (1 << 26)
                if status in (0, 2):
                    self._apply_block_update((bx, by, bz), 0)
                return

            if pid == 0x08:
                pos_long = struct.unpack_from(">q", data, off)[0]; off += 8
                face = data[off]; off += 1
                item_id = struct.unpack_from(">h", data, off)[0]; off += 2
                if item_id >= 0:
                    off += 1  # count
                    off += 2  # damage
                    nbt_len = struct.unpack_from(">h", data, off)[0]; off += 2
                    if nbt_len > 0:
                        off += nbt_len
                bx = pos_long >> 38
                by = (pos_long >> 26) & 0xFFF
                bz = pos_long << 38 >> 38
                if bx >= (1 << 25): bx -= (1 << 26)
                if bz >= (1 << 25): bz -= (1 << 26)
                if face <= 5:
                    dx, dy, dz = self._offset_from_face(face)
                    block_id = item_id if item_id in (1, 2) else 1
                    self._apply_block_update((bx + dx, by + dy, bz + dz), block_id)
                return
        except Exception as e:
            self._status(f"Play packet error: {e}")

    def _enter_play(self, client_sock, username):
        entity_id = self.entity_seq
        self.entity_seq += 1
        client_sock.settimeout(0.2)
        client = {"sock": client_sock, "entity_id": entity_id, "username": username}
        with self.clients_lock:
            self.clients.append(client)

        spawn_x = self.level.w // 2
        spawn_z = self.level.h // 2
        top_y = 0
        for y in range(self.level.d - 1, -1, -1):
            if int(self.level.blocks[spawn_x, y, spawn_z]) != 0:
                top_y = y
                break
        feet_y = float(top_y + 2)

        join_payload = (
            struct.pack(">i", entity_id) +
            struct.pack(">B", 1) +      # creative for free movement
            struct.pack(">b", 0) +      # overworld
            struct.pack(">B", 2) +      # difficulty
            struct.pack(">B", 8) +
            write_string("default") +
            struct.pack(">?", False)
        )
        self._send_packet(client_sock, 0x01, join_payload)
        self._send_packet(client_sock, 0x05, pack_block_position(spawn_x, top_y + 1, spawn_z))
        abilities = struct.pack(">Bff", 0x07, 0.05, 0.1)
        self._send_packet(client_sock, 0x39, abilities)
        poslook = (
            struct.pack(">d", spawn_x + 0.5) +
            struct.pack(">d", feet_y) +
            struct.pack(">d", spawn_z + 0.5) +
            struct.pack(">f", 0.0) +
            struct.pack(">f", 0.0) +
            struct.pack(">B", 0)
        )
        self._send_packet(client_sock, 0x08, poslook)
        self._send_all_chunks(client_sock)

        try:
            while self.running:
                try:
                    pid, data, off = recv_packet(client_sock, -1)
                    self._handle_play_packet(pid, data, off)
                except socket.timeout:
                    continue
        except Exception:
            pass
        finally:
            with self.clients_lock:
                if client in self.clients:
                    self.clients.remove(client)
            try:
                client_sock.close()
            except:
                pass

    def _send_all_chunks(self, client_sock):
        max_cx = self.level.w // 16
        max_cz = self.level.h // 16
        for cx in range(max_cx):
            for cz in range(max_cz):
                payload = self._build_chunk_payload(cx, cz)
                if payload is not None:
                    self._send_packet(client_sock, 0x21, payload)

    def _build_chunk_payload(self, chunk_x, chunk_z):
        primary_bitmask = 0
        data = bytearray()
        for section_y in range(16):
            y0 = section_y * 16
            if y0 >= self.level.d:
                break
            has_blocks = False
            packed = bytearray(8192)
            for by_local in range(16):
                wy = y0 + by_local
                if wy >= self.level.d:
                    continue
                for bz_local in range(16):
                    wz = chunk_z * 16 + bz_local
                    if wz >= self.level.h:
                        continue
                    for bx_local in range(16):
                        wx = chunk_x * 16 + bx_local
                        if wx >= self.level.w:
                            continue
                        bid = int(self.level.blocks[wx, wy, wz])
                        if bid == 0:
                            continue
                        has_blocks = True
                        i = bx_local | (bz_local << 4) | (by_local << 8)
                        packed[i * 2] = (bid & 0xF) << 4
                        packed[i * 2 + 1] = (bid >> 4) & 0xFF
            if not has_blocks:
                continue
            primary_bitmask |= (1 << section_y)
            data.extend(packed)
            data.extend(bytearray(2048))
            data.extend(bytearray([0xFF]) * 2048)

        if primary_bitmask == 0:
            return None

        data.extend(bytearray([1]) * 256)
        payload = (
            struct.pack(">i", chunk_x) +
            struct.pack(">i", chunk_z) +
            struct.pack(">?", True) +
            struct.pack(">H", primary_bitmask) +
            write_varint(len(data)) +
            bytes(data)
        )
        return payload

    def broadcast_block_change(self, pos, block_id):
        payload = pack_block_position(*pos) + write_varint(int(block_id) << 4)
        with self.clients_lock:
            clients = list(self.clients)
        dead = []
        for client in clients:
            try:
                self._send_packet(client["sock"], 0x23, payload)
            except:
                dead.append(client)
        if dead:
            with self.clients_lock:
                for client in dead:
                    if client in self.clients:
                        self.clients.remove(client)

# =====================================================================
# IP INPUT SCREEN
# =====================================================================

class IPInputScreen:
    def __init__(self, font, width, height):
        self.font = font
        self.width = width
        self.height = height
        self.ip_text = "localhost"
        self.port_text = "25565"
        self.username_text = "Player"
        self.active_field = 0  # 0=ip, 1=port, 2=username
        self.done = False
        self.cancelled = False

    def handle_event(self, ev):
        if ev.type == KEYDOWN:
            if ev.key == K_ESCAPE:
                self.cancelled = True
                return
            if ev.key == K_RETURN:
                if self.active_field < 2:
                    self.active_field += 1
                else:
                    self.done = True
                return
            if ev.key == K_TAB:
                self.active_field = (self.active_field + 1) % 3
                return
            if ev.key == K_BACKSPACE:
                if self.active_field == 0: self.ip_text = self.ip_text[:-1]
                elif self.active_field == 1: self.port_text = self.port_text[:-1]
                else: self.username_text = self.username_text[:-1]
                return
            if ev.unicode and ev.unicode.isprintable():
                if self.active_field == 0 and len(self.ip_text) < 50:
                    self.ip_text += ev.unicode
                elif self.active_field == 1 and len(self.port_text) < 6:
                    self.port_text += ev.unicode
                elif self.active_field == 2 and len(self.username_text) < 16:
                    self.username_text += ev.unicode

    def draw(self, draw_text_fn):
        glClearColor(0.05, 0.05, 0.1, 1.0)
        glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)
        glDisable(GL_TEXTURE_2D)
        
        cx = self.width // 2
        draw_text_fn("JOIN JAVA SERVER", cx, 150, (255, 200, 50), center=True)
        
        colors_label = [(200,200,200)] * 3
        colors_val = [(100,200,255), (100,200,255), (100,200,255)]
        colors_val[self.active_field] = (255, 255, 100)
        
        labels = ["IP Address:", "Port:", "Username:"]
        values = [self.ip_text, self.port_text, self.username_text]
        
        for i, (label, val) in enumerate(zip(labels, values)):
            y = 280 + i * 90
            draw_text_fn(label, cx - 200, y, colors_label[i])
            cursor = "_" if self.active_field == i and int(time.time() * 2) % 2 == 0 else ""
            draw_text_fn(val + cursor, cx - 200, y + 40, colors_val[i])
        
        draw_text_fn("TAB / ENTER: next field", cx, 590, (150, 150, 150), center=True)
        draw_text_fn("ENTER (last field): connect", cx, 630, (150, 255, 150), center=True)
        draw_text_fn("ESC: back", cx, 670, (200, 100, 100), center=True)
        
        draw_text_fn("[Offline mode - online-mode=false required]", cx, 720, (120, 120, 120), center=True)
        
        glEnable(GL_TEXTURE_2D)

# =====================================================================
# RUBYDUNG PRINCIPAL
# =====================================================================

class RubyDung:
    def __init__(self):
        pygame.init()
        pygame.display.set_mode((WIDTH, HEIGHT), DOUBLEBUF | OPENGL | RESIZABLE)
        pygame.display.set_caption("Nanocraft")
        pygame.font.init()
        
        self.STATE_MENU = 0
        self.STATE_GAME = 1
        self.STATE_IP_INPUT = 2
        self.STATE_JAVA_GAME = 3
        self.state = self.STATE_MENU
        
        try: self.font = pygame.font.Font("typo.ttf", 32)
        except: self.font = pygame.font.SysFont('Consolas', 28)
        
        try: self.font_small = pygame.font.Font("typo.ttf", 20)
        except: self.font_small = pygame.font.SysFont('Consolas', 18)

        self.level = Level(MAP_W, MAP_D, MAP_H)
        self.player = Player(self.level)
        
        self.tex = self.load_texture("terrain.png")
        self.skin_tex = self.load_texture("skin.png")
        
        self.chunks = [Chunk(x, y, z, self.level)
                       for x in range(0, MAP_W, CHUNK_SIZE)
                       for y in range(0, MAP_D, CHUNK_SIZE)
                       for z in range(0, MAP_H, CHUNK_SIZE)]
        
        glEnable(GL_TEXTURE_2D)
        glEnable(GL_DEPTH_TEST)
        glEnable(GL_CULL_FACE)
        
        self.sock = None
        self.remote_player_pos = None
        self.pending_blocks = []
        
        # Java client state is intentionally separate from the classic solo/LAN mode.
        self.java_client = None
        self.java_chunks = {}  # (cx, cy, cz) -> Chunk render object
        self.java_status = "Not connected"
        self.java_loading = False
        self.java_loading_start = 0.0
        self.last_java_block_refresh = 0.0
        self.java_block_burst_until = 0.0
        self.java_last_ip = None
        self.java_last_port = None
        self.java_last_username = None
        self.java_lan_server = None
        self.java_visible_cache = {}
        self.java_visible_cache_epoch = 0
        self.hotbar_blocks = [("Stone", 1, (120, 120, 120)), ("Grass", 2, (80, 170, 80))]
        self.selected_hotbar = 0
        self.hotbar_counts = {1: 999, 2: 999}
        self.last_java_break_time = 0.0
        self.pending_java_break = None
        self.ip_screen = None
        self.current_fps = 0.0
        glViewport(0, 0, WIDTH, HEIGHT)

    def resize_window(self, width, height):
        global WIDTH, HEIGHT
        WIDTH = max(320, int(width))
        HEIGHT = max(240, int(height))
        pygame.display.set_mode((WIDTH, HEIGHT), DOUBLEBUF | OPENGL | RESIZABLE)
        glViewport(0, 0, WIDTH, HEIGHT)
        if self.ip_screen:
            self.ip_screen.width = WIDTH
            self.ip_screen.height = HEIGHT

    def is_java_survival(self):
        return bool(self.java_client and self.java_client.gamemode == 0)

    def get_visible_hotbar_entries(self):
        if self.is_java_survival():
            return [entry for entry in self.hotbar_blocks if entry[1] != 1]
        return list(self.hotbar_blocks)

    def get_selected_block_id(self):
        entries = self.get_visible_hotbar_entries()
        return entries[self.selected_hotbar][1]

    def reset_java_survival_inventory(self):
        self.hotbar_counts = {1: 0, 2: 0}

    def add_hotbar_resource(self, block_id, amount=1):
        if block_id in self.hotbar_counts:
            self.hotbar_counts[block_id] += amount

    def consume_hotbar_resource(self, block_id, amount=1):
        if not self.is_java_survival():
            return True
        current = self.hotbar_counts.get(block_id, 0)
        if current < amount:
            return False
        self.hotbar_counts[block_id] = current - amount
        return True

    def get_java_survival_break_time(self, block_id):
        # Match Minecraft hand-breaking time closely for the two supported blocks.
        if block_id == 1:
            return 4.0
        if block_id == 2:
            return 4.0
        return 0.75

    def select_hotbar_slot(self, slot):
        entries = self.get_visible_hotbar_entries()
        self.selected_hotbar = max(0, min(len(entries) - 1, int(slot)))
        if self.java_client:
            self.java_client.send_held_item_change(self.selected_hotbar)
            if self.java_client.gamemode == 1:
                self.java_client.send_creative_inventory_action(
                    36 + self.selected_hotbar,
                    self.get_selected_block_id(),
                    64,
                    0,
                )

    def apply_local_java_block_change(self, pos, block_id):
        if not self.java_client:
            return
        x, y, z = pos
        key = self.level.get_java_chunk_key(x, y, z)
        with self.java_client.chunk_lock:
            if block_id == 0:
                self.level.java_blocks.pop((x, y, z), None)
                bucket = self.level.java_chunk_blocks.get(key)
                if bucket is not None:
                    bucket.pop((x, y, z), None)
                    if not bucket:
                        del self.level.java_chunk_blocks[key]
                        self.level.java_visible_chunk_blocks.pop(key, None)
            else:
                self.level.java_blocks[(x, y, z)] = block_id
                if key not in self.level.java_chunk_blocks:
                    self.level.java_chunk_blocks[key] = {}
                self.level.java_chunk_blocks[key][(x, y, z)] = block_id
            self.level.java_visible_chunk_blocks.pop(key, None)
            self.java_client._mark_dirty_block_chunks(x, y, z)

    def process_pending_java_break(self):
        if not self.pending_java_break or not self.java_client:
            return
        if time.time() < self.pending_java_break["ready_time"]:
            return

        pos = self.pending_java_break["pos"]
        face = self.pending_java_break["face"]
        block_id = self.pending_java_break["block_id"]
        self.pending_java_break = None

        if self.java_client.send_dig_status(2, pos, face):
            self.apply_local_java_block_change(pos, 0)
            if self.is_java_survival() and block_id != 1:
                self.add_hotbar_resource(block_id, 1)

    # ---- Classic LAN mode ----
    def broadcast_host(self):
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        udp.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        while True:
            try: udp.sendto(f"NANOCRAFT_HOST:{PORT}".encode(), ('255.255.255.255', PORT))
            except: pass
            pygame.time.wait(2000)

    def discover_host(self):
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        udp.bind(('', PORT))
        while True:
            try:
                data, addr = udp.recvfrom(1024)
                msg = data.decode()
                if msg.startswith("NANOCRAFT_HOST"): return addr[0]
            except: pass

    def network_thread(self, is_host):
        try:
            if is_host:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM); s.bind(('0.0.0.0', PORT)); s.listen(1)
                self.sock, _ = s.accept()
            else:
                self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                host_ip = self.discover_host()
                self.sock.connect((host_ip, PORT))
            self.sock.setblocking(False)
            while True:
                packet = {"pos": (self.player.x, self.player.y, self.player.z, self.player.yRot), "blocks": self.pending_blocks[:]}
                self.sock.send(pickle.dumps(packet))
                self.pending_blocks = []
                try:
                    d = self.sock.recv(4096)
                    if d:
                        data = pickle.loads(d)
                        self.remote_player_pos = data["pos"]
                        for b_pos, b_type in data["blocks"]:
                            self.set_block(b_pos, b_type, sync=False)
                except: pass
                pygame.time.wait(20)
        except: pass

    def start_host_lan_services(self):
        threading.Thread(target=self.network_thread, args=(True,), daemon=True).start()
        threading.Thread(target=self.broadcast_host, daemon=True).start()
        try:
            if not self.java_lan_server:
                self.java_lan_server = JavaLanServer(self.level, on_block_update=self.set_block)
            port = self.java_lan_server.start()
            print(f"[HOST] Java LAN server started on {port}")
        except Exception as e:
            print(f"[HOST] Java LAN server error: {e}")

    def stop_host_lan_services(self):
        if self.java_lan_server:
            self.java_lan_server.stop()
            self.java_lan_server = None

    # ---- Java connection ----
    def start_java_connection(self, ip, port, username):
        """Start a connection to a Java Minecraft server."""
        self.java_last_ip = ip
        self.java_last_port = int(port)
        self.java_last_username = username

        if self.java_client:
            self.java_client.disconnect()

        # Reset the local level so Java chunk data can replace the fixed solo/LAN terrain.
        self.level = Level(MAP_W, MAP_D, MAP_H)
        self.level.java_mode = True
        self.level.java_terrain_tex = self.tex
        self.player = Player(self.level)
        self.java_chunks = {}
        self.java_visible_cache = {}
        self.java_visible_cache_epoch = 0
        self.reset_java_survival_inventory()
        self.last_java_break_time = 0.0
        self.pending_java_break = None
        self.java_status = "Connecting..."
        self.java_loading = True
        self.java_loading_start = time.time()
        self.last_java_block_refresh = 0.0
        self.java_block_burst_until = 0.0
        
        self.java_client = MinecraftJavaClient(
            ip, int(port), username,
            self.level, self.player,
            self.on_java_status
        )
        self.java_client.connect()

    def on_java_status(self, msg):
        self.java_status = msg
        print("[JAVA]", msg)

    def get_or_create_java_chunk(self, cx, cy, cz):
        """Called only from the main OpenGL thread."""
        key = (cx, cy, cz)
        if key not in self.java_chunks:
            # Delay display list allocation until the render thread touches the chunk.
            c = Chunk.__new__(Chunk)
            c.pos = (cx, cy, cz)
            c.level = self.level
            c.list_id = None
            c.dirty = True
            self.java_chunks[key] = c
            self.java_visible_cache_epoch += 1
            self.java_visible_cache.clear()
        return self.java_chunks[key]

    def update_java_chunks(self):
        """Flush block-only Java updates to the renderer at a fixed interval."""
        if not self.java_client:
            return 0

        now = time.time()
        if now - self.last_java_block_refresh < JAVA_BLOCK_UPDATE_INTERVAL:
            return 0
        self.last_java_block_refresh = now
        
        with self.java_client.chunk_lock:
            dirty = set(self.java_client.dirty_chunks)
            self.java_client.dirty_chunks.clear()

        if dirty:
            self.java_block_burst_until = now + JAVA_BLOCK_BURST_SECONDS
        
        for (cx, cy, cz) in dirty:
            c = self.get_or_create_java_chunk(cx, cy, cz)
            c.dirty = True
        return len(dirty)

    def reload_java_nearby_chunks(self):
        if not self.java_last_ip or self.java_last_port is None or not self.java_last_username:
            return
        self.java_status = "Reloading chunks from server..."
        self.start_java_connection(self.java_last_ip, self.java_last_port, self.java_last_username)

    # ---- Drawing ----
    def draw_crosshair(self):
        glMatrixMode(GL_PROJECTION); glPushMatrix(); glLoadIdentity()
        gluOrtho2D(0, WIDTH, HEIGHT, 0)
        glMatrixMode(GL_MODELVIEW); glPushMatrix(); glLoadIdentity()
        glDisable(GL_DEPTH_TEST); glDisable(GL_TEXTURE_2D)
        glColor3f(1, 1, 1)
        cx, cy = WIDTH // 2, HEIGHT // 2; size = 8
        glBegin(GL_LINES)
        glVertex2f(cx-size, cy); glVertex2f(cx+size, cy)
        glVertex2f(cx, cy-size); glVertex2f(cx, cy+size)
        glEnd()
        glEnable(GL_TEXTURE_2D); glEnable(GL_DEPTH_TEST)
        glPopMatrix(); glMatrixMode(GL_PROJECTION); glPopMatrix(); glMatrixMode(GL_MODELVIEW)

    def load_texture(self, filename):
        try:
            surf = pygame.image.load(filename); data = pygame.image.tostring(surf, "RGBA", 1)
            tid = glGenTextures(1); glBindTexture(GL_TEXTURE_2D, tid)
            glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, surf.get_width(), surf.get_height(), 0, GL_RGBA, GL_UNSIGNED_BYTE, data)
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST)
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST)
            return tid
        except: return None

    def draw_cube_face(self, x0, y0, z0, x1, y1, z1, u0, v0, u1, v1):
        u0, v0, u1, v1 = u0/16, v0/32, u1/16, v1/32
        glBegin(GL_QUADS)
        glTexCoord2f(u0, v1); glVertex3f(x0, y0, z0)
        glTexCoord2f(u1, v1); glVertex3f(x1, y0, z1)
        glTexCoord2f(u1, v0); glVertex3f(x1, y1, z1)
        glTexCoord2f(u0, v0); glVertex3f(x0, y1, z0)
        glEnd()

    # Java players and LAN players do not share the same facing fix:
    # LAN keeps the original local protocol orientation, while Java uses the
    # corrected model-facing offset needed for real Minecraft clients.
    def draw_steve(self, pos):
        rx, ry, rz, rr = pos
        glPushMatrix(); glTranslatef(rx, ry-1.6, rz); glRotatef(180.0 - rr, 0, 1, 0)
        glBindTexture(GL_TEXTURE_2D, self.skin_tex)
        self.draw_cube_face(-0.375, 0.0, 0.125, 0.375, 1.75, 0.125, 8, 0, 16, 32)
        self.draw_cube_face(0.375, 0.0, -0.125, -0.375, 1.75, -0.125, 0, 0, 8, 32)
        self.draw_cube_face(-0.375, 0.0, -0.125, -0.375, 1.75, 0.125, 7.5, 0, 8.5, 32)
        self.draw_cube_face(0.375, 0.0, 0.125, 0.375, 1.75, -0.125, 7.5, 0, 8.5, 32)
        self.draw_cube_face(-0.375, 1.75, 0.125, 0.375, 1.75, -0.125, 8, 0, 9, 1)
        glPopMatrix()
        if self.tex: glBindTexture(GL_TEXTURE_2D, self.tex)

    def draw_steve_lan(self, pos):
        rx, ry, rz, rr = pos
        glPushMatrix(); glTranslatef(rx, ry-1.6, rz); glRotatef(-rr, 0, 1, 0)
        glEnable(GL_TEXTURE_2D)
        glColor3f(1.0, 1.0, 1.0)
        glBindTexture(GL_TEXTURE_2D, self.skin_tex)
        self.draw_cube_face(-0.375, 0.0, 0.125, 0.375, 1.75, 0.125, 8, 0, 16, 32)
        self.draw_cube_face(0.375, 0.0, -0.125, -0.375, 1.75, -0.125, 0, 0, 8, 32)
        self.draw_cube_face(-0.375, 0.0, -0.125, -0.375, 1.75, 0.125, 7.5, 0, 8.5, 32)
        self.draw_cube_face(0.375, 0.0, 0.125, 0.375, 1.75, -0.125, 7.5, 0, 8.5, 32)
        self.draw_cube_face(-0.375, 1.75, 0.125, 0.375, 1.75, -0.125, 8, 0, 9, 1)
        glPopMatrix()
        if self.tex: glBindTexture(GL_TEXTURE_2D, self.tex)

    def draw_text(self, text, x, y, color=(255, 255, 255), center=False, small=False):
        font = self.font_small if small else self.font
        surf = font.render(text, True, color)
        if center: x -= surf.get_width() // 2
        data = pygame.image.tostring(surf, "RGBA", True)
        glMatrixMode(GL_PROJECTION); glPushMatrix(); glLoadIdentity()
        gluOrtho2D(0, WIDTH, HEIGHT, 0)
        glMatrixMode(GL_MODELVIEW); glPushMatrix(); glLoadIdentity()
        glDisable(GL_DEPTH_TEST); glEnable(GL_BLEND); glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
        glDisable(GL_TEXTURE_2D)
        glRasterPos2i(x, y)
        glDrawPixels(surf.get_width(), surf.get_height(), GL_RGBA, GL_UNSIGNED_BYTE, data)
        glDisable(GL_BLEND); glEnable(GL_DEPTH_TEST); glEnable(GL_TEXTURE_2D)
        glPopMatrix(); glMatrixMode(GL_PROJECTION); glPopMatrix(); glMatrixMode(GL_MODELVIEW)

    def draw_fps(self, is_java=False):
        y = 110 if is_java else 45
        self.draw_text(f"FPS: {self.current_fps:.0f}", 10, y, (255, 255, 255), small=True)

    def draw_ui_block_tile(self, block_id, x, y, size=26):
        if not self.tex:
            return
        s = 0.0625
        u0 = (block_id - 1) * s
        u1 = u0 + s
        v0 = 1.0 - s
        v1 = 1.0

        glMatrixMode(GL_PROJECTION); glPushMatrix(); glLoadIdentity()
        gluOrtho2D(0, WIDTH, HEIGHT, 0)
        glMatrixMode(GL_MODELVIEW); glPushMatrix(); glLoadIdentity()
        glDisable(GL_DEPTH_TEST)
        glDisable(GL_CULL_FACE)
        glEnable(GL_BLEND)
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
        glEnable(GL_TEXTURE_2D)
        glBindTexture(GL_TEXTURE_2D, self.tex)
        glColor3f(1.0, 1.0, 1.0)
        glBegin(GL_QUADS)
        glTexCoord2f(u0, v1); glVertex2f(x, y)
        glTexCoord2f(u1, v1); glVertex2f(x + size, y)
        glTexCoord2f(u1, v0); glVertex2f(x + size, y + size)
        glTexCoord2f(u0, v0); glVertex2f(x, y + size)
        glEnd()
        glDisable(GL_BLEND)
        glEnable(GL_CULL_FACE)
        glEnable(GL_DEPTH_TEST)
        glPopMatrix(); glMatrixMode(GL_PROJECTION); glPopMatrix(); glMatrixMode(GL_MODELVIEW)

    def draw_hotbar(self):
        entries = self.get_visible_hotbar_entries()
        slot_w = 90
        slot_h = 54
        gap = 10
        total_w = len(entries) * slot_w + (len(entries) - 1) * gap
        x0 = (WIDTH - total_w) // 2
        y0 = HEIGHT - slot_h - 18

        glMatrixMode(GL_PROJECTION); glPushMatrix(); glLoadIdentity()
        gluOrtho2D(0, WIDTH, HEIGHT, 0)
        glMatrixMode(GL_MODELVIEW); glPushMatrix(); glLoadIdentity()
        glDisable(GL_DEPTH_TEST)
        glDisable(GL_TEXTURE_2D)

        for i, (_, _, color) in enumerate(entries):
            sx = x0 + i * (slot_w + gap)
            selected = (i == self.selected_hotbar)
            border = (1.0, 1.0, 0.6) if selected else (0.25, 0.25, 0.25)
            fill = tuple(min(1.0, c / 255.0 + (0.12 if selected else 0.0)) for c in color)
            icon_x0 = sx + 18
            icon_y0 = y0 + 10
            icon_x1 = icon_x0 + 26
            icon_y1 = icon_y0 + 26

            glColor3f(*border)
            glBegin(GL_QUADS)
            glVertex2f(sx, y0); glVertex2f(sx + slot_w, y0)
            glVertex2f(sx + slot_w, y0 + slot_h); glVertex2f(sx, y0 + slot_h)
            glEnd()

            glColor3f(0.08, 0.08, 0.08)
            glBegin(GL_QUADS)
            glVertex2f(sx + 3, y0 + 3); glVertex2f(sx + slot_w - 3, y0 + 3)
            glVertex2f(sx + slot_w - 3, y0 + slot_h - 3); glVertex2f(sx + 3, y0 + slot_h - 3)
            glEnd()

            glColor3f(*fill)
            glBegin(GL_QUADS)
            glVertex2f(icon_x0, icon_y0); glVertex2f(icon_x1, icon_y0)
            glVertex2f(icon_x1, icon_y1); glVertex2f(icon_x0, icon_y1)
            glEnd()

            if selected:
                glColor3f(0.0, 0.0, 0.0)
                glBegin(GL_QUADS)
                glVertex2f(icon_x0 - 2, icon_y0 - 2); glVertex2f(icon_x1 + 2, icon_y0 - 2)
                glVertex2f(icon_x1 + 2, icon_y0); glVertex2f(icon_x0 - 2, icon_y0)
                glEnd()
                glBegin(GL_QUADS)
                glVertex2f(icon_x0 - 2, icon_y1); glVertex2f(icon_x1 + 2, icon_y1)
                glVertex2f(icon_x1 + 2, icon_y1 + 2); glVertex2f(icon_x0 - 2, icon_y1 + 2)
                glEnd()
                glBegin(GL_QUADS)
                glVertex2f(icon_x0 - 2, icon_y0); glVertex2f(icon_x0, icon_y0)
                glVertex2f(icon_x0, icon_y1); glVertex2f(icon_x0 - 2, icon_y1)
                glEnd()
                glBegin(GL_QUADS)
                glVertex2f(icon_x1, icon_y0); glVertex2f(icon_x1 + 2, icon_y0)
                glVertex2f(icon_x1 + 2, icon_y1); glVertex2f(icon_x1, icon_y1)
                glEnd()

        glEnable(GL_DEPTH_TEST)
        glPopMatrix(); glMatrixMode(GL_PROJECTION); glPopMatrix(); glMatrixMode(GL_MODELVIEW)

        for i, (_, block_id, _) in enumerate(entries):
            sx = x0 + i * (slot_w + gap)
            self.draw_text(str(i + 1), sx + 8, y0 + 10, (255, 255, 255), small=True)
            self.draw_ui_block_tile(block_id, sx + 18, y0 + 10, 26)
            if self.is_java_survival():
                count = self.hotbar_counts.get(block_id, 0)
                self.draw_text(str(count), sx + 48, y0 + 28, (230, 230, 230), small=True)

    def get_block_face(self, target, adjacent):
        if not target or not adjacent:
            return 1
        dx = adjacent[0] - target[0]
        dy = adjacent[1] - target[1]
        dz = adjacent[2] - target[2]
        if dy > 0:
            return 1
        if dy < 0:
            return 0
        if dz < 0:
            return 2
        if dz > 0:
            return 3
        if dx < 0:
            return 4
        if dx > 0:
            return 5
        return 1

    def draw_menu(self):
        glClearColor(0.1, 0.1, 0.1, 1.0)
        glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)
        glDisable(GL_TEXTURE_2D)
        self.draw_text("NANOCRAFT", WIDTH//2, 200, (255, 255, 0), center=True)
        self.draw_text("[1] SOLO", WIDTH//2, 350, center=True)
        self.draw_text("[2] HOST LAN", WIDTH//2, 420, center=True)
        self.draw_text("[3] JOIN LAN", WIDTH//2, 490, center=True)
        self.draw_text("[4] JOIN JAVA SERVER", WIDTH//2, 560, center=True)
        glEnable(GL_TEXTURE_2D)

    def _java_loading_done(self):
        if not self.java_client:
            return False
        if self.java_client.chunks_received < JAVA_LOADING_MIN_CHUNKS:
            return False
        if self.java_client.last_chunk_time <= 0:
            return False
        if (time.time() - self.java_client.last_chunk_time) < JAVA_LOADING_IDLE_SECONDS:
            return False
        for _, _, c in self._get_java_visible_chunks():
            if c.dirty or not c.list_id:
                return False
        return True

    def _get_java_visible_chunks(self, extra_chunks=0, cull_behind=False):
        px, py, pz = self.player.x, self.player.y, self.player.z
        pcx = int(math.floor(px / CHUNK_SIZE_RENDER)) * CHUNK_SIZE_RENDER
        pcy = int(math.floor(py / CHUNK_SIZE_RENDER)) * CHUNK_SIZE_RENDER
        pcz = int(math.floor(pz / CHUNK_SIZE_RENDER)) * CHUNK_SIZE_RENDER
        yaw_bucket = int((self.player.yRot % 360.0) / 20.0) if cull_behind else 0
        cache_key = (pcx, pcy, pcz, yaw_bucket, extra_chunks, cull_behind, self.java_visible_cache_epoch)
        cached = self.java_visible_cache.get(cache_key)
        if cached is not None:
            return cached
        look_x = math.sin(math.radians(self.player.yRot))
        look_z = -math.cos(math.radians(self.player.yRot))
        visible_chunks = []

        horiz_radius = (JAVA_RENDER_DIST_CHUNKS + extra_chunks) * CHUNK_SIZE_RENDER
        vert_radius = (JAVA_VERTICAL_RENDER_CHUNKS + extra_chunks) * CHUNK_SIZE_RENDER

        for cx in range(pcx - horiz_radius, pcx + horiz_radius + CHUNK_SIZE_RENDER, CHUNK_SIZE_RENDER):
            for cy in range(pcy - vert_radius, pcy + vert_radius + CHUNK_SIZE_RENDER, CHUNK_SIZE_RENDER):
                for cz in range(pcz - horiz_radius, pcz + horiz_radius + CHUNK_SIZE_RENDER, CHUNK_SIZE_RENDER):
                    c = self.java_chunks.get((cx, cy, cz))
                    if not c:
                        continue

                    chunk_center_x = cx + 8
                    chunk_center_z = cz + 8
                    dx = chunk_center_x - px
                    dz = chunk_center_z - pz
                    dy = cy - pcy

                    if cull_behind and extra_chunks <= 0 and dx * look_x + dz * look_z < -24:
                        continue
                    visible_chunks.append((dx * dx + dz * dz + dy * dy, (cx, cy, cz), c))

        visible_chunks.sort(key=lambda item: item[0])
        self.java_visible_cache = {cache_key: visible_chunks}
        return visible_chunks

    def _build_java_chunks_with_budget(self, visible_chunks, max_builds, frame_budget_ms):
        built_now = 0
        deadline = time.perf_counter() + (frame_budget_ms / 1000.0)
        for _, key, c in visible_chunks:
            if built_now >= max_builds or time.perf_counter() >= deadline:
                break
            if not (c.dirty or not c.list_id):
                continue
            try:
                if c.list_id is None or (isinstance(c.list_id, int) and c.list_id <= 0):
                    c.list_id = glGenLists(1)
                c.build()
                built_now += 1
            except Exception as e:
                print(f"[BUILD ERROR] chunk {key}: {e}")
                c.dirty = True
        return built_now

    def respawn_local_player_to_center(self):
        center_x = MAP_W / 2.0
        center_z = MAP_H / 2.0
        center_block_x = min(MAP_W - 1, max(0, int(center_x)))
        center_block_z = min(MAP_H - 1, max(0, int(center_z)))
        spawn_y = 33.0
        for y in range(MAP_D - 1, -1, -1):
            if self.level.blocks[center_block_x, y, center_block_z] > 0:
                spawn_y = y + 3.62
                break

        self.player.x = center_x
        self.player.y = spawn_y
        self.player.z = center_z
        self.player.xd = 0
        self.player.yd = 0
        self.player.zd = 0
        feet_y = spawn_y - 1.62
        self.player.bb = AABB(center_x-0.3, feet_y, center_z-0.3, center_x+0.3, feet_y+1.8, center_z+0.3)
        self.player.onGround = False

    def render_game_world(self, is_java=False):
        """Rendu 3D commun"""
        if is_java and self.java_loading:
            if self.java_client:
                self.java_client._drain_decoded_chunks(12)
            dirty = []
            if self.java_client:
                with self.java_client.chunk_lock:
                    dirty = list(self.java_client.dirty_chunks)
                    self.java_client.dirty_chunks.clear()
            for key in dirty:
                c = self.get_or_create_java_chunk(*key)
                c.dirty = True

            self._build_java_chunks_with_budget(
                self._get_java_visible_chunks(extra_chunks=JAVA_PREFETCH_CHUNKS),
                JAVA_LOADING_BUILD_BUDGET,
                JAVA_LOADING_FRAME_BUDGET_MS,
            )

        if is_java and self.java_loading and not self._java_loading_done():
            glClearColor(0.05, 0.08, 0.12, 1.0)
            glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)
            loaded = self.java_client.chunks_received if self.java_client else 0
            prepared = 0
            total_visible = 0
            for _, _, c in self._get_java_visible_chunks():
                total_visible += 1
                if not c.dirty and c.list_id:
                    prepared += 1
            self.draw_text("LOADING JAVA WORLD...", WIDTH//2, 260, (255, 220, 120), center=True)
            self.draw_text(f"Chunks received: {loaded}", WIDTH//2, 340, (180, 220, 255), center=True)
            self.draw_text(f"Chunks ready: {prepared}/{total_visible}", WIDTH//2, 380, (180, 255, 180), center=True)
            self.draw_text(self.java_status, WIDTH//2, 400, (200, 200, 200), center=True, small=True)
            return

        if is_java and self.java_loading:
            self.java_loading = False
            self.java_status = "Loading complete - entering game"

        dx, dy = pygame.mouse.get_rel()
        self.player.yRot += dx * 0.15
        self.player.xRot = max(-90, min(90, self.player.xRot + dy * 0.15))
        self.process_pending_java_break()

        if not is_java:
            self.player.tick()
            if self.player.y < -10:
                self.respawn_local_player_to_center()
        else:
            if self.java_client and self.java_client.gamemode == 0:
                self.player.tick()
            else:
                self._java_player_tick()
        
        glMatrixMode(GL_PROJECTION); glLoadIdentity()
        gluPerspective(70, WIDTH/HEIGHT, 0.1, 512)
        glMatrixMode(GL_MODELVIEW)
        glClearColor(0.5, 0.8, 1.0, 1.0)
        glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)
        glLoadIdentity()
        glRotatef(self.player.xRot, 1, 0, 0)
        glRotatef(self.player.yRot, 0, 1, 0)
        glTranslatef(-self.player.x, -self.player.y, -self.player.z)
        
        if is_java:
            if self.java_client:
                self.java_client._drain_decoded_chunks(8)
            glDisable(GL_TEXTURE_2D)

            # Batch block placement/break updates so only touched chunks refresh,
            # and not on every single frame.
            self.update_java_chunks()

            visible_chunks = self._get_java_visible_chunks(extra_chunks=JAVA_RENDER_KEEP_CHUNKS)
            rebuilds_left = JAVA_REBUILD_BUDGET
            if time.time() < self.java_block_burst_until:
                rebuilds_left = max(rebuilds_left, JAVA_BLOCK_REBUILD_BUDGET)

            self._build_java_chunks_with_budget(
                visible_chunks,
                rebuilds_left,
                JAVA_REBUILD_FRAME_BUDGET_MS,
            )

            for _, key, c in visible_chunks:
                if c.list_id:
                    try:
                        glCallList(c.list_id)
                    except Exception as e:
                        print(f"[DRAW ERROR] chunk {key}: {e}")

            glColor3f(1.0, 1.0, 1.0)
            glEnable(GL_TEXTURE_2D)

        else:
            glBindTexture(GL_TEXTURE_2D, self.tex)
            for c in self.chunks:
                if abs(c.pos[0]-self.player.x) < RENDER_DIST and abs(c.pos[2]-self.player.z) < RENDER_DIST:
                    if c.dirty:
                        c.build()
                    glCallList(c.list_id)

            if self.remote_player_pos:
                self.draw_steve_lan(self.remote_player_pos)

        if is_java and self.java_client:
            with self.java_client.entity_lock:
                java_players = [
                    (p["x"], p["y"], p["z"], p.get("head_yaw", p.get("yaw", 0.0)))
                    for p in self.java_client.remote_players.values()
                ]
            for pos in java_players:
                self.draw_steve(pos)

        self.draw_crosshair()
        
        if is_java:
            # HUD status
            self.draw_text(self.java_status, 10, 30, (255, 255, 100), small=True)
            self.draw_text("R - reload chunks", WIDTH - 230, 30, (255, 255, 255), small=True)
            nc = len(self.java_chunks)
            self.draw_text(f"Chunks: {nc}  Pos: ({self.player.x:.1f}, {self.player.y:.1f}, {self.player.z:.1f})", 
                          10, 55, (200, 200, 200), small=True)
            nb = len(self.level.java_blocks)
            self.draw_text(f"Blocks in memory: {nb}", 10, 80, (200, 200, 200), small=True)
            self.draw_fps(is_java=True)

        if self.state in [self.STATE_GAME, self.STATE_JAVA_GAME]:
            self.draw_hotbar()

    def _java_player_tick(self):
        """Mouvement flottant en mode Java (spectateur)"""
        keys = pygame.key.get_pressed()
        xa, za, ya = 0, 0, 0
        if keys[K_z] or keys[K_w]: za -= 1
        if keys[K_s]: za += 1
        if keys[K_q] or keys[K_a]: xa -= 1
        if keys[K_d]: xa += 1
        if keys[K_SPACE]: ya += 1
        if keys[K_LSHIFT] or keys[K_LCTRL]: ya -= 1

        speed = 0.5
        m = math.sqrt(xa*xa + za*za)
        if m > 0.01:
            xa /= m; za /= m

        s = math.sin(math.radians(self.player.yRot))
        c = math.cos(math.radians(self.player.yRot))
        self.player.x += (xa * c - za * s) * speed
        self.player.z += (za * c + xa * s) * speed
        self.player.y += ya * speed

    def run(self):
        clock = pygame.time.Clock()
        
        while True:
            for ev in pygame.event.get():
                if ev.type == QUIT:
                    if self.java_client: self.java_client.disconnect()
                    self.stop_host_lan_services()
                    return
                if ev.type == VIDEORESIZE:
                    self.resize_window(ev.w, ev.h)
                    continue
                if ev.type == KEYDOWN:
                    if ev.key == K_ESCAPE:
                        if self.state in [self.STATE_GAME, self.STATE_JAVA_GAME]:
                            pygame.mouse.set_visible(True)
                            pygame.event.set_grab(False)
                            self.state = self.STATE_MENU
                            if self.java_client:
                                self.java_client.disconnect()
                                self.java_client = None
                                self.level = Level(MAP_W, MAP_D, MAP_H)
                                self.player = Player(self.level)
                            self.stop_host_lan_services()
                        elif self.state == self.STATE_IP_INPUT:
                            self.state = self.STATE_MENU
                        else:
                            if self.java_client: self.java_client.disconnect()
                            self.stop_host_lan_services()
                            return

                    if self.state in [self.STATE_GAME, self.STATE_JAVA_GAME]:
                        if ev.key in [K_1, K_KP1]:
                            self.select_hotbar_slot(0)
                        elif ev.key in [K_2, K_KP2]:
                            self.select_hotbar_slot(1)
                    
                    if self.state == self.STATE_MENU:
                        if ev.key in [K_1, K_KP1]:
                            self.state = self.STATE_GAME
                            pygame.mouse.set_visible(False); pygame.event.set_grab(True)
                        elif ev.key in [K_2, K_KP2]:
                            self.state = self.STATE_GAME
                            self.start_host_lan_services()
                            pygame.mouse.set_visible(False); pygame.event.set_grab(True)
                        elif ev.key in [K_3, K_KP3]:
                            self.state = self.STATE_GAME
                            threading.Thread(target=self.network_thread, args=(False,), daemon=True).start()
                            pygame.mouse.set_visible(False); pygame.event.set_grab(True)
                        elif ev.key in [K_4, K_KP4]:
                            self.state = self.STATE_IP_INPUT
                            self.ip_screen = IPInputScreen(self.font, WIDTH, HEIGHT)
                    
                    elif self.state == self.STATE_IP_INPUT:
                        if self.ip_screen:
                            self.ip_screen.handle_event(ev)

                    elif self.state == self.STATE_JAVA_GAME and ev.key == K_r:
                        self.reload_java_nearby_chunks()
                    
                    elif self.state == self.STATE_GAME and ev.type == KEYDOWN:
                        pass
                
                if ev.type == KEYDOWN and self.state == self.STATE_IP_INPUT:
                    pass  # already handled above
                    
                if self.state == self.STATE_GAME and ev.type == MOUSEBUTTONDOWN:
                    t, p = self.get_ray()
                    if ev.button == 1 and t: self.set_block(t, 0)
                    if ev.button == 3 and p: self.set_block(p, self.get_selected_block_id())

                if self.state == self.STATE_JAVA_GAME and ev.type == MOUSEBUTTONDOWN and self.java_client:
                    t, p = self.get_ray()
                    if ev.button == 1 and t:
                        broken_block = self.level.get_block(*t)
                        face = self.get_block_face(t, p)
                        if self.is_java_survival():
                            if broken_block == 0:
                                continue
                            if self.java_client.send_dig_status(0, t, face):
                                self.last_java_break_time = time.time()
                                break_time = self.get_java_survival_break_time(broken_block)
                                self.pending_java_break = {
                                    "pos": t,
                                    "face": face,
                                    "block_id": broken_block,
                                    "ready_time": self.last_java_break_time + break_time,
                                }
                        elif self.java_client.send_dig_block(t, face):
                            self.apply_local_java_block_change(t, 0)
                    if ev.button == 3 and t and p:
                        selected_block = self.get_selected_block_id()
                        if self.is_java_survival() and not self.consume_hotbar_resource(selected_block, 1):
                            self.java_status = "No blocks in selected slot"
                            continue
                        if self.java_client.send_place_block(t, self.get_block_face(t, p), selected_block):
                            self.apply_local_java_block_change(p, self.get_selected_block_id())
                        elif self.is_java_survival():
                            self.add_hotbar_resource(selected_block, 1)
            
            # Check whether the Java connection form was completed.
            if self.state == self.STATE_IP_INPUT and self.ip_screen:
                if self.ip_screen.cancelled:
                    self.state = self.STATE_MENU
                    self.ip_screen = None
                elif self.ip_screen.done:
                    ip = self.ip_screen.ip_text
                    port = self.ip_screen.port_text
                    username = self.ip_screen.username_text
                    self.ip_screen = None
                    self.state = self.STATE_JAVA_GAME
                    self.start_java_connection(ip, port, username)
                    pygame.mouse.set_visible(False)
                    pygame.event.set_grab(True)
            
            # Rendering
            if self.state == self.STATE_MENU:
                self.draw_menu()
            elif self.state == self.STATE_IP_INPUT:
                if self.ip_screen:
                    self.ip_screen.draw(self.draw_text)
            elif self.state == self.STATE_GAME:
                self.render_game_world(is_java=False)
            elif self.state == self.STATE_JAVA_GAME:
                self.render_game_world(is_java=True)

            if self.state != self.STATE_JAVA_GAME:
                self.draw_fps(is_java=False)
            
            pygame.display.flip()
            clock.tick(60)
            self.current_fps = clock.get_fps()

    def get_ray(self):
        x, y, z = self.player.x, self.player.y, self.player.z
        ry, rx = math.radians(self.player.yRot), math.radians(self.player.xRot)
        dx, dy, dz = math.sin(ry)*math.cos(rx), -math.sin(rx), -math.cos(ry)*math.cos(rx)
        for _ in range(120):
            x += dx*0.05; y += dy*0.05; z += dz*0.05
            if self.level.is_solid(x, y, z):
                return (int(x), int(y), int(z)), (int(x-dx*0.05), int(y-dy*0.05), int(z-dz*0.05))
        return None, None

    def set_block(self, pos, b, sync=True):
        x, y, z = pos
        if 0 <= x < MAP_W and 0 <= y < MAP_D and 0 <= z < MAP_H:
            if self.level.blocks[x,y,z] == b: return
            self.level.blocks[x,y,z] = b
            if self.java_lan_server:
                self.java_lan_server.broadcast_block_change(pos, b)
            if sync: self.pending_blocks.append((pos, b))
            for c in self.chunks:
                if x >= c.pos[0] and x < c.pos[0]+16 and y >= c.pos[1] and y < c.pos[1]+16 and z >= c.pos[2] and z < c.pos[2]+16:
                    c.dirty = True

if __name__ == "__main__":
    RubyDung().run()

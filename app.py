import pygame
from pygame.locals import *
from OpenGL.GL import *
from OpenGL.GLU import *
import numpy as np
import math
import socket
import threading
import pickle

# --- CONFIGURATION ---
WIDTH, HEIGHT = 1024, 768
CHUNK_SIZE = 16
MAP_W, MAP_D, MAP_H = 128, 64, 128 
RENDER_DIST = 96 
PORT = 5555

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

    def is_solid(self, x, y, z):
        if 0 <= x < self.w and 0 <= y < self.d and 0 <= z < self.h:
            return self.blocks[int(x), int(y), int(z)] > 0
        return False

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

class Chunk:
    def __init__(self, x0, y0, z0, level):
        self.pos = (x0, y0, z0)
        self.level = level
        self.list_id = glGenLists(1)
        self.dirty = True

    def build(self):
        glNewList(self.list_id, GL_COMPILE)
        glBegin(GL_QUADS)
        x0, y0, z0 = self.pos
        s = 0.0625 
        for x in range(x0, x0 + CHUNK_SIZE):
            for y in range(y0, y0 + CHUNK_SIZE):
                for z in range(z0, z0 + CHUNK_SIZE):
                    b = self.level.blocks[x, y, z]
                    if b == 0: continue
                    u, v = (b - 1) * s, 1.0 - s
                    if not self.level.is_solid(x, y+1, z):
                        glColor3f(1.0, 1.0, 1.0); glTexCoord2f(u, v+s); glVertex3f(x, y+1, z); glTexCoord2f(u, v); glVertex3f(x, y+1, z+1); glTexCoord2f(u+s, v); glVertex3f(x+1, y+1, z+1); glTexCoord2f(u+s, v+s); glVertex3f(x+1, y+1, z)
                    if not self.level.is_solid(x, y-1, z):
                        glColor3f(0.6, 0.6, 0.6); glTexCoord2f(u+s, v+s); glVertex3f(x+1, y, z); glTexCoord2f(u+s, v); glVertex3f(x+1, y, z+1); glTexCoord2f(u, v); glVertex3f(x, y, z+1); glTexCoord2f(u, v+s); glVertex3f(x, y, z)
                    if not self.level.is_solid(x, y, z+1):
                        glColor3f(0.8, 0.8, 0.8); glTexCoord2f(u, v); glVertex3f(x, y, z+1); glTexCoord2f(u+s, v); glVertex3f(x+1, y, z+1); glTexCoord2f(u+s, v+s); glVertex3f(x+1, y+1, z+1); glTexCoord2f(u, v+s); glVertex3f(x, y+1, z+1)
                    if not self.level.is_solid(x, y, z-1):
                        glColor3f(0.8, 0.8, 0.8); glTexCoord2f(u+s, v); glVertex3f(x+1, y, z); glTexCoord2f(u, v); glVertex3f(x, y, z); glTexCoord2f(u, v+s); glVertex3f(x, y+1, z); glTexCoord2f(u+s, v+s); glVertex3f(x+1, y+1, z)
                    if not self.level.is_solid(x+1, y, z):
                        glColor3f(0.7, 0.7, 0.7); glTexCoord2f(u+s, v); glVertex3f(x+1, y, z+1); glTexCoord2f(u, v); glVertex3f(x+1, y, z); glTexCoord2f(u, v+s); glVertex3f(x+1, y+1, z); glTexCoord2f(u+s, v+s); glVertex3f(x+1, y+1, z+1)
                    if not self.level.is_solid(x-1, y, z):
                        glColor3f(0.7, 0.7, 0.7); glTexCoord2f(u, v); glVertex3f(x, y, z); glTexCoord2f(u+s, v); glVertex3f(x, y, z+1); glTexCoord2f(u+s, v+s); glVertex3f(x, y+1, z+1); glTexCoord2f(u, v+s); glVertex3f(x, y+1, z)
        glEnd(); glEndList()
        self.dirty = False

class RubyDung:
    def __init__(self):
        pygame.init()
        pygame.display.set_mode((WIDTH, HEIGHT), DOUBLEBUF | OPENGL)
        pygame.display.set_caption("Nanocraft")
        pygame.font.init()
        self.STATE_MENU, self.STATE_GAME = 0, 1
        self.state = self.STATE_MENU
        try: self.font = pygame.font.Font("typo.ttf", 32)
        except: self.font = pygame.font.SysFont('Arial', 32)
        self.level = Level(MAP_W, MAP_D, MAP_H)
        self.player = Player(self.level)
        self.tex = self.load_texture("terrain.png")
        self.skin_tex = self.load_texture("skin.png")
        self.chunks = [Chunk(x, y, z, self.level) for x in range(0, MAP_W, CHUNK_SIZE) for y in range(0, MAP_D, CHUNK_SIZE) for z in range(0, MAP_H, CHUNK_SIZE)]
        glEnable(GL_TEXTURE_2D); glEnable(GL_DEPTH_TEST); glEnable(GL_CULL_FACE)
        self.sock, self.remote_player_pos = None, None
        self.pending_blocks = []

    # --- AJOUT LAN ---
    def broadcast_host(self):
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        udp.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        while True:
            try:
                msg = f"NANOCRAFT_HOST:{PORT}"
                udp.sendto(msg.encode(), ('255.255.255.255', PORT))
            except:
                pass
            pygame.time.wait(2000)

    def discover_host(self):
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        udp.bind(('', PORT))
        while True:
            try:
                data, addr = udp.recvfrom(1024)
                msg = data.decode()
                if msg.startswith("NANOCRAFT_HOST"):
                    return addr[0]
            except:
                pass
    # --- FIN AJOUT ---

    def draw_crosshair(self):
        glMatrixMode(GL_PROJECTION)
        glPushMatrix()
        glLoadIdentity()
        gluOrtho2D(0, WIDTH, HEIGHT, 0)

        glMatrixMode(GL_MODELVIEW)
        glPushMatrix()
        glLoadIdentity()

        glDisable(GL_DEPTH_TEST)
        glDisable(GL_TEXTURE_2D)

        glColor3f(1, 1, 1)

        cx, cy = WIDTH // 2, HEIGHT // 2
        size = 8

        glBegin(GL_LINES)
        glVertex2f(cx - size, cy)
        glVertex2f(cx + size, cy)
        glVertex2f(cx, cy - size)
        glVertex2f(cx, cy + size)
        glEnd()

        glEnable(GL_TEXTURE_2D)
        glEnable(GL_DEPTH_TEST)

        glPopMatrix()
        glMatrixMode(GL_PROJECTION)
        glPopMatrix()
        glMatrixMode(GL_MODELVIEW)

    def load_texture(self, filename):
        try:
            surf = pygame.image.load(filename); data = pygame.image.tostring(surf, "RGBA", 1)
            tid = glGenTextures(1); glBindTexture(GL_TEXTURE_2D, tid)
            glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, surf.get_width(), surf.get_height(), 0, GL_RGBA, GL_UNSIGNED_BYTE, data)
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST); glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST)
            return tid
        except: return None

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

    def draw_cube_face(self, x0, y0, z0, x1, y1, z1, u0, v0, u1, v1):
        u0, v0, u1, v1 = u0/16, v0/32, u1/16, v1/32
        glBegin(GL_QUADS)
        glTexCoord2f(u0, v1); glVertex3f(x0, y0, z0)
        glTexCoord2f(u1, v1); glVertex3f(x1, y0, z1)
        glTexCoord2f(u1, v0); glVertex3f(x1, y1, z1)
        glTexCoord2f(u0, v0); glVertex3f(x0, y1, z0)
        glEnd()

    def draw_steve(self, pos):
        rx, ry, rz, rr = pos
        glPushMatrix(); glTranslatef(rx, ry-1.6, rz); glRotatef(-rr, 0, 1, 0)
        glBindTexture(GL_TEXTURE_2D, self.skin_tex)
        self.draw_cube_face(-0.375, 0.0, 0.125, 0.375, 1.75, 0.125, 8, 0, 16, 32)
        self.draw_cube_face(0.375, 0.0, -0.125, -0.375, 1.75, -0.125, 0, 0, 8, 32)
        self.draw_cube_face(-0.375, 0.0, -0.125, -0.375, 1.75, 0.125, 7.5, 0, 8.5, 32)
        self.draw_cube_face(0.375, 0.0, 0.125, 0.375, 1.75, -0.125, 7.5, 0, 8.5, 32)
        self.draw_cube_face(-0.375, 1.75, 0.125, 0.375, 1.75, -0.125, 8, 0, 9, 1)
        glPopMatrix(); glBindTexture(GL_TEXTURE_2D, self.tex)

    def draw_text(self, text, x, y, color=(255, 255, 255), center=False):
        surf = self.font.render(text, True, color)
        if center: x -= surf.get_width() // 2
        data = pygame.image.tostring(surf, "RGBA", True)
        glMatrixMode(GL_PROJECTION); glPushMatrix(); glLoadIdentity(); gluOrtho2D(0, WIDTH, HEIGHT, 0)
        glMatrixMode(GL_MODELVIEW); glPushMatrix(); glLoadIdentity()
        glDisable(GL_DEPTH_TEST); glEnable(GL_BLEND); glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
        glRasterPos2i(x, y); glDrawPixels(surf.get_width(), surf.get_height(), GL_RGBA, GL_UNSIGNED_BYTE, data)
        glDisable(GL_BLEND); glEnable(GL_DEPTH_TEST); glPopMatrix(); glMatrixMode(GL_PROJECTION); glPopMatrix(); glMatrixMode(GL_MODELVIEW)

    def draw_menu(self):
        glClearColor(0.1, 0.1, 0.1, 1.0); glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)
        glDisable(GL_TEXTURE_2D)
        self.draw_text("NANOCRAFT", WIDTH//2, 200, (255, 255, 0), center=True)
        self.draw_text("[1] SOLO", WIDTH//2, 350, center=True)
        self.draw_text("[2] HOST LAN", WIDTH//2, 420, center=True)
        self.draw_text("[3] JOIN LAN", WIDTH//2, 490, center=True)
        glEnable(GL_TEXTURE_2D)

    def run(self):
        clock = pygame.time.Clock()
        while True:
            for ev in pygame.event.get():
                if ev.type == QUIT: return
                if ev.type == KEYDOWN:
                    if ev.key == K_ESCAPE: return
                    if self.state == self.STATE_MENU:
                        if ev.key in [K_1, K_KP1]: self.state = self.STATE_GAME
                        elif ev.key in [K_2, K_KP2]:
                            self.state = self.STATE_GAME
                            threading.Thread(target=self.network_thread, args=(True,), daemon=True).start()
                            threading.Thread(target=self.broadcast_host, daemon=True).start()
                        elif ev.key in [K_3, K_KP3]:
                            self.state = self.STATE_GAME
                            threading.Thread(target=self.network_thread, args=(False,), daemon=True).start()
                        if self.state == self.STATE_GAME:
                            pygame.mouse.set_visible(False); pygame.event.set_grab(True)
                if self.state == self.STATE_GAME and ev.type == MOUSEBUTTONDOWN:
                    t, p = self.get_ray()
                    if ev.button == 1 and t: self.set_block(t, 0)
                    if ev.button == 3 and p: self.set_block(p, 1)

            if self.state == self.STATE_MENU:
                self.draw_menu()
            else:
                dx, dy = pygame.mouse.get_rel()
                self.player.yRot += dx * 0.15; self.player.xRot = max(-90, min(90, self.player.xRot + dy * 0.15))
                self.player.tick()
                glMatrixMode(GL_PROJECTION); glLoadIdentity(); gluPerspective(70, WIDTH/HEIGHT, 0.1, 512); glMatrixMode(GL_MODELVIEW)
                glClearColor(0.5, 0.8, 1.0, 1.0); glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT); glLoadIdentity()
                glRotatef(self.player.xRot, 1, 0, 0); glRotatef(self.player.yRot, 0, 1, 0); glTranslatef(-self.player.x, -self.player.y, -self.player.z)
                
                glBindTexture(GL_TEXTURE_2D, self.tex)
                for c in self.chunks:
                    if abs(c.pos[0]-self.player.x) < RENDER_DIST and abs(c.pos[2]-self.player.z) < RENDER_DIST:
                        if c.dirty: c.build()
                        glCallList(c.list_id)
                
                if self.remote_player_pos:
                    self.draw_steve(self.remote_player_pos)
                self.draw_crosshair()
                    
            pygame.display.flip(); clock.tick(60)

    def get_ray(self):
        x, y, z = self.player.x, self.player.y, self.player.z
        ry, rx = math.radians(self.player.yRot), math.radians(self.player.xRot)
        dx, dy, dz = math.sin(ry)*math.cos(rx), -math.sin(rx), -math.cos(ry)*math.cos(rx)
        for _ in range(120):
            x += dx*0.05; y += dy*0.05; z += dz*0.05
            if self.level.is_solid(x, y, z): return (int(x), int(y), int(z)), (int(x-dx*0.05), int(y-dy*0.05), int(z-dz*0.05))
        return None, None

    def set_block(self, pos, b, sync=True):
        x, y, z = pos
        if 0 <= x < MAP_W and 0 <= y < MAP_D and 0 <= z < MAP_H:
            if self.level.blocks[x,y,z] == b: return
            self.level.blocks[x,y,z] = b
            if sync: self.pending_blocks.append((pos, b))
            for c in self.chunks:
                if x >= c.pos[0] and x < c.pos[0]+16 and y >= c.pos[1] and y < c.pos[1]+16 and z >= c.pos[2] and z < c.pos[2]+16: c.dirty = True

if __name__ == "__main__": RubyDung().run()
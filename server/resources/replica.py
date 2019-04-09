from enum import Enum
import ipaddress
import asyncio
import socket
import secrets
import json
from aiohttp import web
import aiohttp
import random
import board

class Mode(Enum):
    BACKUP = 0
    PRIMARY = 1

class State(Enum):
    NORMAL = 0
    VIEW_CHANGE = 1
    RECOVERING = 2
    
class Timer:
    def __init__(self, timeout, callback, loop):
        self._timeout = timeout
        self._callback = callback
        self._loop = loop
        # self._task = asyncio.ensure_future(self._job())

    async def _job(self):
        await asyncio.sleep(self._timeout)
        await self._callback()

    def cancel(self):
        self._task.cancel()
    
    def start(self, timeout = None, callback = None):
        if callback is not None:
            self.callback = callback
        if timeout is not None:
            self.timeout = timeout
        self._task = asyncio.ensure_future(self._job(), loop=self._loop)

    def restart(self):
        self._task.cancel()
        self._task = asyncio.ensure_future(self._job(), loop=self._loop)

class replica:
    
    def __init__(self, routing_ip):
        self.current_mode = Mode.BACKUP
        self.current_state = State.NORMAL
        self.other_replicas = []
        self.all_replicas = []
        self.client_list = []
        self.message_out_queue = asyncio.Queue()
        self.routing_layer = routing_ip
        self.n_view = 0
        self.n_view_old = 0
        self.n_commit = 0
        self.n_operation = 0
        self.n_recovery_messages = 0
        self.n_start_view_change_messages = 0
        self.n_do_view_change_messages = 0
        self.primary_recovery_response = False
        self.game_running = False
        #The log will be a list of the events that have occurred, the lookup will correspond to the Operation number of the request being served
        self.log = []

        #get Ip of the local computer
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        #s.getsockname() has the local ip address at [0] and the local port at [1]
        self.local_ip = s.getsockname()[0]
        print("IP address: ", self.local_ip)
        s.close()
        self.all_replicas.append(self.local_ip)

        #start the local loop to allow for asyncio (starts the server)       
        self.loop = asyncio.get_event_loop()

        self.loop.create_task(self.http_server_start())
        self.loop.create_task(self.request_primary_ip())

        try:
            self.loop.run_forever()
        except ConnectionError:
            pass
        except:
            self.loop.close()
        

    async def start_recovery(self):
        self.current_state = State.RECOVERING
        self.n_recovery_messages = 0
        
        self.timer.cancel()
        #Send broadcast to all replicas with random nonce and its address
        self.recovery_nonce = secrets.randbits(32)
        message = json.dumps({
            "Type": "Recover",
            "N_replica": self.local_ip,
            "Nonce": self.recovery_nonce
	    })
        self.replica_broadcast("post", "Recover", message)

    async def recovery_response(self, request):
        if self.current_state == State.RECOVERING:
            body = await request.json()
            txt = json.loads(body)
            if txt["Nonce"] == self.recovery_nonce:
                self.n_recovery_messages +=1
                if request.remote == self.primary:
                    # save state info
                    self.log = txt["Log"]###we will need to sort this log bit out
                    self.n_commit = txt["N_Commit"]
                    self.n_operation = txt["N_Operation"]
                    self.n_view = txt["N_View"]
                    self.primary_recovery_response = True
                if self.n_recovery_messages >= int(len(self.other_replicas)/2) and self.primary_recovery_response:
                    self.n_recovery_messages = 0
                    self.primary_recovery_response = False
                    self.current_state = State.NORMAL
                    self.timer.start()
        return web.Response()


    async def start_view_change(self, request):
        #recieves this message from other nodes to start the process.
        
        # Disable timer
        self.timer.cancel()
        
        # Save old view number before view change was called
        if self.current_state != State.VIEW_CHANGE:
            self.n_view_old = self.n_view
            
        # Advance view number
        self.n_view += 1
    
        # Set state to view change
        self.current_state = State.VIEW_CHANGE

        # Create json for StartViewChange
        message = json.dumps({
            "N_View": self.n_view,
            "N_replica": self.local_ip
        })

        # Broadcast StartViewChange to all other replicas
        self.replica_broadcast("post", "StartViewChange", message)

        # Check to see if view number is the same
        reply = await request.json()
        txt = json.loads(reply)
        if self.n_view < txt["N_View"]:
            self.start_view_change

        if self.n_view == txt["N_View"]:
            self.n_start_view_change_messages += 1

            # Wait for f StartViewMessages from other replicas
            # Get new primary replica
            new_primary = self.get_new_primary_replica
            if self.n_start_view_change_messages >= int(len(self.other_replicas)/2):
                self.do_view_change

    async def send_view_change(self):
        #change to view change mode
        #send out view change message
        print("timer expired")
        # TODO: implement
        pass

    async def send_commit(self):
        #send out the commit message as a heartbeat
        # print("sending commit")
        msg = json.dumps({"Type": "Commit", "N_View": self.n_view, "N_Commit": self.n_commit})
        resp = await self.replica_broadcast("post", "Commit", msg)
        self.timer.start()

    async def replica_broadcast(self, req_type, req_location, msg):
        for rep in self.other_replicas:
            await self.send_message(str(rep),req_type, req_location, msg)

    async def request_primary_ip(self):
        resp = await self.session.get("http://" + self.routing_layer + ":5000/join")
        txt = await resp.text()
        a_resp = json.loads(txt)
        self.primary = a_resp['Primary_IP']
        if a_resp['Primary_IP'] != self.local_ip:
            self.other_replicas.append(a_resp['Primary_IP'])
            self.all_replicas.append(a_resp['Primary_IP'])

            #connect to primary and ask for updated replica list
            msg = json.dumps({"Type": "GetReplicaList", "IP": self.local_ip})
            await self.send_message(self.primary, "get", "GetReplicaList", msg)

            #start the heartbeat expectiation from the primary.
            self.timer = Timer(10, self.send_view_change, self.loop)
            self.timer.start(10, self.send_view_change)
        else:
            #start a timer to send out a commit message (basically as a heartbeat)
            self.timer = Timer(7, self.send_commit, self.loop)
            self.timer.start(7, self.send_commit)

    async def get_new_primary_replica(self, old_ip):
        index = self.all_replicas.index(old_ip)
        return self.all_replicas[index + 1]
        

    async def send_message(self, ip_addr, req_type, req_location, data):
        if req_type == "post":
            await self.session.post("http://" + ip_addr + ":9999/" + req_location, data = json.dumps(data))
        if req_type == "get":
            await self.session.get("http://" + ip_addr + ":9999/" + req_location, data = json.dumps(data))
            

    async def player_move(self, request):
        #check if the move has already been made (op number)
        msg = await request.json()
        if type(msg) == dict:
            text = msg
        else:
            text = json.loads(msg)
        
        #primary sends out player move to backups, they add into the gamestate
        if self.local_ip == self.primary:
            if len(self.log) >= text['N_Operation']:
                #TODO:resend the operation with the GameUpdate package
                pass
            else:
                # add fields needed for the replicas (commit number op number etc.)
                await self.replica_broadcast("post", "PlayerMovement", msg)
                #TODO:apply update
                
                #update commit number
                return web.Response()
        


        #backups recieve the player move and adds it to the gamestate, then replies when it's finished
        else:
            #TODO:apply update
            #update operation number
            #update commit number
            return web.Response()
    
    async def player_move_ok(self, request):
        #TODO: implement
        pass


    async def client_join(self, request):
        #client has joined up
        #check for a running game
        if not self.game_running:
            if request.remote not in self.client_list:
                self.client_list.append(request.remote)
            self.client_list.sort()
            if self.local_ip == self.primary:
                msg = await request.json()
                if type(msg) == dict:
                    text = msg
                else:
                    text = json.loads(msg)
                resp = json.dumps({
                    "Type": "ClientJoinOK",
                    "Client_ID": request.remote,
                    "N_Request": text['N_Request']})
                return web.Response(body = resp)
            else:
                return web.Response()
        else:
            return web.Response(status = 400)

    async def readied_up(self, request):
        #add the user's ready state
        #TODO: implement
        pass

    async def start_game(self):
        #finalize the servers on game start
        #send the message to the clients to begin the game

        ####################### IMPORTANT #######################
        # Current logic is the board is 2x the number of players
        # Change below if we want logic to change
        ####################### IMPORTANT #######################
        size = int(len(self.client_list)) * 2
        game_board = board.Board(size)
        gamestate = game_board.get_full_gamestate()
        for i in self.client_list:
            start = json.dumps({
                "Type": "GameStart",
                "Client_ID": i,
                "Gamestate": gamestate
            })
            self.session.post("http://" + self.routing_layer + ":5000/join", data=start)

    async def compute_gamestate(self, request):
        #compute gamestate and return message
        #TODO: implement
        pass
    
    async def receive_gamestate(self, request):
        #TODO: implement
        pass

    async def do_view_change(self, request):
        #send the doviewchange message
        reply = json.dumps({
            "Type": "DoViewChange",
            "N_View": self.n_view,
            "Log": self.log,
            "N_View_Old": self.n_view_old,
            "N_Operation": self.n_operation,
            "N_Commit": self.n_commit,
            "N_replica": self.local_ip
            })

        # Send DoViewChange to new primary
        new_primary = self.get_new_primary_replica
        self.send_message(new_primary, "post", "DoViewChange", reply)

        # If replica is primary, wait for f + 1 DoViewChange responses and update information
        if self.primary == self.local_ip:
            reply = await request.json()
            txt = reply.loads(reply)
            
            if self.n_view_old == txt["N_View_Old"] and self.n_operation < txt["N_Operation"]:
                self.log = txt["Log"]

            if self.n_commit < txt["N_Commit"]:
                self.n_commit = txt["N_Commit"]

            if self.n_do_view_change_messages >= int(len(self.all_replicas) / 2):
                # Change status back to normal and send startview message to other replicas
                self.current_state = State.NORMAL

                # StartView json
                startview_message = json.dumps({
                    "Type": "StartView",
                    "N_View": self.n_view,
                    "Log": self.log,
                    "N_Operation": self.n_operation,
                    "N_Commit": self.n_commit
                })

                # Broadcast message to other replicas
                self.replica_broadcast("post", "StartView", startview_message)


    async def apply_commit(self, request):
        #recieve the commit message, and apply if necessary.
        self.timer.cancel()
        msg = await request.json()
        if type(msg) == dict:
            text = msg
        else:
            text = json.loads(msg)
        if text["N_View"] > self.n_view:
            await self.start_recovery()
        if text["N_Commit"] > self.n_commit:
            await self.start_state_transfer()
        
        return web.Response()
            
        self.timer.start()
        #don't update client about this one.

    async def start_view(self, request):
        body = await request.json()
        txt = json.loads(body)
        self.n_view = txt['N_View']
        self.Log = txt['Log']
        self.n_operation = txt['N_Operation']
        self.n_commit = txt['N_Commit']
        self.primary = request.remote
        self.current_state = State.NORMAL
        return web.Request()
    
    async def start_state_transfer(self):
        #send state transfer
        msg = {
            "Type": "GetState",
            "N_View":self.n_view,
            "N_Operation":self.n_operation,
            "N_Replica":self.local_ip
        }
        # self.send_message(self.other_replicas[random.randint(0,len(self.other_replicas))], "get", "GetState", msg)
        tmp_list = self.other_replicas
        # print([i for i in tmp_list])
        print(random.sample(tmp_list,1))
        await self.send_message(random.sample(tmp_list, 1)[0], "get", "GetState", msg)


    async def get_state(self, request):

        #TODO: set op number to commit number, clear logs before that
        self.n_operation = self.n_commit
        self.log = self.log[:self.n_operation+1]
        msg = json.dumps({
            "Type": "NewState",
            "N_View":self.n_view,
            "Log":self.log[-1],
            "N_Operation":self.n_operation,
            "N_Commit":self.n_commit})
        return web.Response(body = msg)

    async def recovery_help(self, request):
        #send back the recover message
        body = await request.json()
        txt = json.loads(body)
        if self.primary == self.local_ip:
            #return the intense answer
            reply = json.dumps({
                "Type": "RecoverResponse",
                "N_View": self.n_view,
                "Nonce": txt['Nonce'],
                "Log": self.log,
                "N_Operation": self.n_operation,
                "N_Commit": self.n_commit
            })
            self.send_message(request.remote, "post", "RecoverResponse", reply)
            return web.Response()
        else:
            #return the small answer
            msg = json.dumps({
                "Type": "RecoveryResponse",
                "N_View":self.n_view,
                "Nonce":txt['Nonce'],
                "Log":"Nil",
                "N_Operation":"Nil",
                "N_Commit":"Nil"})
            self.send_message(request.remote, "post", "RecoverResponse", msg)
            return web.Response()
         
        

    async def replica_list(self, request):
        #format the replica list and return it to the backup
        if self.local_ip == self.primary:
            if request.remote != self.local_ip:
                if request.remote not in self.all_replicas:
                    self.all_replicas.append(request.remote)
                    print("Added\t" + request.remote)
                if request.remote not in self.other_replicas:
                    self.other_replicas.append(request.remote)

            body = json.dumps({"Type": "UpdateReplicaList", "Replica_List": [i for i in self.all_replicas]})
            resp = await self.replica_broadcast("post", "UpdateReplicaList", body)
            return web.Response()
        else: 
            return web.Response(status = 400, body = json.dumps({"Primary_IP": self.primary}))

    async def update_replicas(self, request):
        if self.local_ip == self.primary:
            body = await request.json()
            body = json.loads(body)
            newList = body["Replica_List"]
            for i in newList:
                if i not in self.all_replicas:
                    self.all_replicas.append(i)
                if i not in self.other_replicas and i != self.local_ip:
                    self.other_replicas.append(i)
            return web.Response()
        else: 
            return web.Response(status = 400, body = json.dumps({"Primary_IP": self.primary}))

    # This starts the http server and listens for the specified http requests
    async def http_server_start(self):
        self.session = aiohttp.ClientSession()
        self.app = web.Application()
        # add routes that we will need for this system with the corresponding coroutines
        self.app.add_routes([web.post('/PlayerMovement', self.player_move),
                            web.post('/ClientJoin', self.client_join),
                            web.post('/Ready', self.readied_up),
                            
                            web.post('/StartViewChange', self.start_view_change),
                            web.post('/DoViewChange', self.do_view_change),
                            web.post('/StartView', self.start_view),
                            web.post('/Recover', self.recovery_help),
                            web.post('/RecoveryResponse', self.recovery_response),
                            web.post('/GetState', self.get_state),
                            web.post('/Commit', self.apply_commit),
                            web.post('/PlayerMoveOK', self.player_move_ok),
                            web.get('/GetReplicaList', self.replica_list),
                            web.post('/UpdateReplicaList', self.update_replicas),
                            web.get('/ComputeGamestate', self.compute_gamestate),
                            web.get('/Gamestate', self.receive_gamestate)])
        self.runner = aiohttp.web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, self.local_ip, 9999)
        await self.site.start()



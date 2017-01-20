# Copyright (C) 2011 Nippon Telegraph and Telephone Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

## Sample configuration file 
#
#[DEFAULT]
#
##wsapi_host=<hostip>
##wsapi_port=<port:8080>
##ofp_listen_host=<hostip>
#ofp_tcp_listen_port=6653
#observe_links=True
#explicit_drop=False

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet
from ryu.lib.packet import ethernet, arp, ipv4
from ryu.lib.packet import ether_types
import ryu.topology.api as api

from Queue import PriorityQueue

import time, cProfile, pstats, StringIO

class NixSimpleSwitch13(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(NixSimpleSwitch13, self).__init__(*args, **kwargs)
        self.logger.info("%s: Starting app", time.time())

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        # install table-miss flow entry
        #
        # We specify NO BUFFER to max_len of the output action due to
        # OVS bug. At this moment, if we specify a lesser number, e.g.,
        # 128, OVS will send Packet-In with invalid buffer_id and
        # truncated packet data. In that case, we cannot output packets
        # correctly.  The bug has been fixed in OVS v2.1.0.
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                          ofproto.OFPCML_NO_BUFFER)]
        self.add_flow(datapath, 0, match, actions)

    def add_flow(self, datapath, priority, match, actions, buffer_id=None):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS,
                                             actions)]
        if buffer_id:
            mod = parser.OFPFlowMod(datapath=datapath, buffer_id=buffer_id,
                                    priority=priority, match=match,
                                    instructions=inst)
        else:
            mod = parser.OFPFlowMod(datapath=datapath, priority=priority,
                                    match=match, instructions=inst)
        datapath.send_msg(mod)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):        
        # If you hit this you might want to increase
        # the "miss_send_length" of your switch
        if ev.msg.msg_len < ev.msg.total_len:
            self.logger.debug("packet truncated: only %s of %s bytes",
                              ev.msg.msg_len, ev.msg.total_len)
        msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        in_port = msg.match['in_port']

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocols(ethernet.ethernet)[0]

        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            # ignore lldp packet
            return

        pr,start = self.enableProf()

        dst = eth.dst
        src = eth.src

        dpid = datapath.id
        
        # Figure out environment
        links = api.get_all_link(self)
        switches = api.get_all_switch(self)
        hosts = api.get_all_host(self)
        
        arp_pkt = None
        if eth.ethertype == ether_types.ETH_TYPE_ARP:
            arp_pkt = pkt.get_protocols(arp.arp)[0]
            if arp_pkt.opcode == arp.ARP_REQUEST:                
                # Send to ARP proxy. Cannot perform NIx routing until both hosts
                # are known by the controller
                self.ArpProxy (msg.data, datapath, in_port, links, switches, hosts)
            elif arp_pkt.opcode == arp.ARP_REPLY:
                self.ArpReply (msg.data, datapath, arp_pkt.dst_ip, links, switches, hosts)
            self.disableProf(pr,start,"ARP")
            return
        
        #self.logger.info("%s: packet in %s %s %s %s", time.time(), dpid, src, dst, in_port)
        
        # Start nix vector code        
        numNodes = len(switches) + len(hosts)
        src_ip = ''
        dst_ip = ''
        srcNode = None
        dstNode = None
        if eth.ethertype == ether_types.ETH_TYPE_ARP:
            arp_pkt = pkt.get_protocols(arp.arp)[0]
            src_ip = arp_pkt.src_ip
            dst_ip = arp_pkt.dst_ip
        elif eth.ethertype == ether_types.ETH_TYPE_IP:
            ipv4_pkt = pkt.get_protocols(ipv4.ipv4)[0]
            src_ip = ipv4_pkt.src
            dst_ip = ipv4_pkt.dst
        for host in hosts:
            if src_ip == host.ipv4[0]:
                srcNode = host
            if dst_ip == host.ipv4[0]:
                dstNode = host

        if srcNode is None or dstNode is None:
            self.ArpProxy (msg.data, datapath, in_port, links, switches, hosts)
            self.disableProf(pr,start,"UNKDST")
            return
                    
        srcSwitch = [switch for switch in switches if switch.dp.id == srcNode.port.dpid][0]
        dstSwitch = [switch for switch in switches if switch.dp.id == dstNode.port.dpid][0]
        parentVec = {}
        foundIt = self.UCS (numNodes, srcSwitch, dstSwitch,
                                       links, switches, hosts, parentVec)
        
        sdnNix = []
        nixVector = []
        if foundIt:
            self.BuildNixVector (parentVec, srcSwitch, dstSwitch, links, switches, hosts, nixVector, sdnNix)

        # Need to send to last switch to send out host port
        sdnNix.insert(0, (dstSwitch, dstNode.port.port_no))
        
        for curNix in sdnNix:
            self.sendNixRules (srcSwitch, curNix[0], curNix[1], eth, msg)

        self.disableProf(pr,start,"COMPLETION")
        
    def ArpProxy (self, data, datapath, in_port, links, switches, hosts):
        for switch in switches:
            # Get all usable ports for this switch and then remove those ports
            # associated with switch-to-switch connections to look at edge ports
            all_ports = set([port.port_no for port in switch.ports if port.is_live()])
            src_ports = set([link.src.port_no for link in links if link.src.dpid == switch.dp.id and link.src.is_live()])
            dst_ports = set([link.dst.port_no for link in links if link.dst.dpid == switch.dp.id and link.dst.is_live()])
            link_ports = src_ports | dst_ports
            non_link_ports = list(all_ports - link_ports)
            
            # Push an ARP request out of the network edges
            for port in non_link_ports:
                if switch.dp != datapath or port != in_port:
                    actions = [switch.dp.ofproto_parser.OFPActionOutput(port)]
                    out = switch.dp.ofproto_parser.OFPPacketOut(datapath=switch.dp,
                                              buffer_id=switch.dp.ofproto.OFP_NO_BUFFER,
                                              in_port=switch.dp.ofproto.OFPP_CONTROLLER,
                                              actions=actions, data=data)
                    
                    #self.logger.info("%s: Sending ARP Request: dpid=%s, port=%s", time.time(), switch.dp.id, port)
                    switch.dp.send_msg(out)

    def ArpReply (self, data, datapath, dst_ip, links, switches, hosts):
        for host in hosts:
            for switch in switches:
                # Push an ARP reply out of the appropriate switch port
                if host.port.dpid == switch.dp.id and host.ipv4.count(dst_ip):
                    actions = [switch.dp.ofproto_parser.OFPActionOutput(host.port.port_no)]
                    out = switch.dp.ofproto_parser.OFPPacketOut(datapath=switch.dp,
                                              buffer_id=switch.dp.ofproto.OFP_NO_BUFFER,
                                              in_port=switch.dp.ofproto.OFPP_CONTROLLER,
                                              actions=actions, data=data)

                    #self.logger.info("%s: Sending ARP Reply: dpid=%s, ip=%s, port=%s", time.time(), switch.dp.id, dst_ip, host.port.port_no)
                    switch.dp.send_msg(out)
        
    def BFS (self, nNodes, srcSwitch, dstSwitch, links, switches, hosts, parentVector):
        greyNodeList = [ srcSwitch ]
        
        parentVector[srcSwitch.dp.id] = greyNodeList[0]
        while len(greyNodeList) != 0:
            currNode = greyNodeList[0]
            if (currNode == dstSwitch):
                return True
              
            for link in links:
                if link.src.dpid == currNode.dp.id:
                    if not link.dst.is_live():
                        continue
                
                    if parentVector.get(link.dst.dpid) == None:
                        parentVector[link.dst.dpid] = currNode
                        currSwitch = [switch for switch in switches if switch.dp.id == link.dst.dpid][0]
                        greyNodeList.append(currSwitch)
            del(greyNodeList[0])
                         
        return False

    def UCS (self, nNodes, srcSwitch, dstSwitch, links, switches, hosts, parentVector):
        parentVector[srcSwitch.dp.id] = srcSwitch
        visited = set()
        q = PriorityQueue()
        q.put((0, srcSwitch, []))

        while q:
            cost,point,path = q.get()
            if point not in visited:
                visited.add(point)

                path = path + [point]
                if point == dstSwitch:
                    for index in range(1,len(path)):
                        parentVector[path[index].dp.id] = path[index-1]
                    self.logger.info("Cost: %f", cost)
                    return True

                for link in links:
                    if link.src.dpid == point.dp.id:
                        if not link.dst.is_live():
                            continue

                        child = [switch for switch in switches if switch.dp.id == link.dst.dpid][0]
                        if child not in visited:
                            total_cost = cost + link.delay
                            q.put((total_cost,child,path))
    
    def BuildNixVector (self, parentVector, srcSwitch, dstSwitch, links, switches, hosts, nixVector, sdnNix):
        if srcSwitch == dstSwitch:
            return True
        
        if parentVector.get(dstSwitch.dp.id) == None:
            return False
        
        parentSwitch = parentVector[dstSwitch.dp.id]
        destId = 0
        totalNeighbors = len([host for host in hosts if host.port.dpid == parentSwitch.dp.id])
        offset = totalNeighbors
        for link in links:
            if link.src.dpid == parentSwitch.dp.id:
                remoteSwitch = [switch for switch in switches if link.dst.dpid == switch.dp.id][0]
                 
                if remoteSwitch == dstSwitch:
                    sdnNix.append((parentSwitch, link.src.port_no))
                    destId = totalNeighbors + offset
                offset += 1
                totalNeighbors += 1
                
        if totalNeighbors > 1:
            newNix = [int(c) for c in self.bin(destId)[2:]]
            nixVector.extend(newNix)
        #self.logger.info("SDN Nix: %s", sdnNix)
        return self.BuildNixVector(parentVector, srcSwitch, parentSwitch, links, switches, hosts, nixVector, sdnNix)
    
    def sendNixRules (self, srcSwitch, switch, port_no, eth, msg):
        ofproto = switch.dp.ofproto
        parser = switch.dp.ofproto_parser

        actions = [switch.dp.ofproto_parser.OFPActionOutput(port_no)]
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS,
                                             actions)]
        
        match = parser.OFPMatch(eth_src=eth.src,
                                eth_dst=eth.dst)
        mod = parser.OFPFlowMod(datapath=switch.dp, priority=1,
                                match=match, instructions=inst)
        switch.dp.send_msg(mod)
                    
        #self.logger.info("%s: Sending Nix rule: dpid=%s, port=%s", time.time(), switch.dp.id, port_no)
        
        if srcSwitch == switch:
            data = None
        
            if msg.buffer_id == ofproto.OFP_NO_BUFFER:
                data = msg.data

            out = parser.OFPPacketOut(datapath=srcSwitch.dp, buffer_id=msg.buffer_id,
                                      in_port=switch.dp.ofproto.OFPP_CONTROLLER,
                                      actions=actions, data=data)
            srcSwitch.dp.send_msg(out)
    
    def bin(self, s):
        return str(s) if s<=1 else bin(s>>1) + str(s&1)

    def enableProf(self):
        pr = cProfile.Profile()
        pr.enable()
        return pr,time.clock()

    def disableProf(self, pr, start, whichcase):
        completion = time.clock() - start
        pr.disable()
        s = StringIO.StringIO()
        ps = pstats.Stats(pr, stream=s)
        ps.print_stats(0)
        self.logger.info("%s\t%f\t%s", whichcase, completion, s.getvalue())

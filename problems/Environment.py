import torch


class DVRPSR_Environment:
    vehicle_feature = 8  # vehicle coordinates(x_i,y_i), veh_time_time_budget, total_travel_time, last_customer,
    # next(destination) customer, last rewards, next rewards
    customer_feature = 4

    # TODO: change pending cost for rewards

    def __init__(self, data, nodes=None, customer_mask=None, edges_attributes=None,
                 pending_cost=1,
                 dynamic_reward=0.2,
                 budget_penalty=10):

        self.vehicle_count = data.vehicle_count
        self.vehicle_speed = data.vehicle_speed
        self.vehicle_time_budget = data.vehicle_time_budget

        self.nodes = data.nodes if nodes is None else nodes
        self.edge_index = data.edges_index
        self.edge_attributes = data.edges_attributes if edges_attributes is None else edges_attributes
        self.init_customer_mask = data.customer_mask if customer_mask is None else customer_mask

        self.minibatch, self.nodes_count, _ = self.nodes.size()
        self.distance_matrix = self.edge_attributes.view((self.minibatch, self.nodes_count, self.nodes_count))
        self.pending_cost = pending_cost
        self.dynamic_reward = dynamic_reward
        self.budget_penalty = budget_penalty

    def _update_current_vehicles(self, dest, customer_index, tau=0):

        # calculate travel time
        # TODO: 1) in real world setting we need to calculate the distance of arc
        # If nodes i and j are directly connected by a road segment (i, j) ∈ A, then t(i,j)=t_ij;
        # otherwise, t(i,j)=t_ik1 +t_k1k2 +...+t_knj, where k1,...,kn ∈ V are the nodes along the
        # shortest path from node i to node j.
        #      2) calculate stating time for each vehicle $\tau $, currently is set to zero

        # update vehicle previous and next customer id
        self.current_vehicle[:, :, 4] = self.current_vehicle[:, :, 5]
        self.current_vehicle[:, :, 5] = customer_index

        # get the distance from current vehicle to its next destination
        dist = torch.zeros((self.minibatch, 1)).to(self.nodes.device)
        for i in range(self.minibatch):
            dist[i, 0] = self.distance_matrix[i][int(self.current_vehicle[i, :, 4])][int(self.current_vehicle[i, :, 5])]

        # total travel time
        tt = dist / self.vehicle_speed

        # customers which are dynamicaly appeared
        dyn_cust = (dest[:, :, 3] > 0).float()

        # budget left while travelling to destination nodes
        budget = tt + dest[:, :, 2]
        # print(budget, tau, tt, dest[:,:,2])

        # update vehicle features based on destination nodes
        self.current_vehicle[:, :, :2] = dest[:, :, :2]
        self.current_vehicle[:, :, 2] -= budget
        self.current_vehicle[:, :, 3] += tt
        self.current_vehicle[:, :, 6] = self.current_vehicle[:, :, 7]
        self.current_vehicle[:, :, 7] = -dist

        # update vehicles states
        self.vehicles = self.vehicles.scatter(1,
                                              self.current_vehicle_index[:, :, None].expand(-1, -1,
                                                                                            self.vehicle_feature),
                                              self.current_vehicle)

        return dist, dyn_cust

    def _done(self, customer_index):

        self.vehicle_done.scatter_(1, self.current_vehicle_index, torch.logical_or((customer_index == 0),
                                                                                   (self.current_vehicle[:, :,
                                                                                    2] <= 0)))
        # print(self.veh_done, cust_idx==0,self.cur_veh[:,:,2]<=0, (cust_idx==0) | (self.cur_veh[:,:,2]<=0))
        self.done = bool(self.vehicle_done.all())

    def _update_mask(self, customer_index):

        self.new_customer = False
        self.served.scatter_(1, customer_index, customer_index > 0)

        # cost for a vehicle to go to customer and back to deport considering service duration
        cost = torch.zeros((self.minibatch, self.nodes_count, 1)).to(self.nodes.device)
        for i in range(self.minibatch):
            for j in range(self.nodes_count):
                dist_vehicle_customer_depot = self.distance_matrix[i][int(self.current_vehicle[i, :, 4])][j] + \
                                              self.distance_matrix[i][j][0]
                cost[i, j] = dist_vehicle_customer_depot

        cost = cost / self.vehicle_speed

        cost += self.nodes[:, :, None, 2]

        overtime_mask = self.current_vehicle[:, :, None, 2] - cost
        overtime_mask = overtime_mask.squeeze(2).unsqueeze(1)
        overtime = torch.zeros_like(self.mask).scatter_(1,
                                                        self.current_vehicle_index[:, :, None].expand(-1, -1,
                                                                                                      self.nodes_count),
                                                        overtime_mask < 0)

        self.mask = self.mask | self.served[:, None, :] | overtime | self.vehicle_done[:, :, None]
        self.mask[:, :, 0] = 0  # depot

    # updating current vehicle to find the next available vehicle
    def _update_next_vehicle(self, veh_index=None):

        if veh_index is None:
            avail = self.vehicles[:, :, 3].clone()
            avail[self.vehicle_done] = float('inf')
            self.current_vehicle_index = avail.argmin(1, keepdim=True)
        else:
            self.current_vehicle_index = veh_index

        self.current_vehicle = self.vehicles.gather(1, self.current_vehicle_index[:, :, None].expand(-1, -1,
                                                                                                     self.vehicle_feature))
        self.current_vehicle_mask = self.mask.gather(1, self.current_vehicle_index[:, :, None].expand(-1, -1,
                                                                                                      self.nodes_count))

    def _update_dynamic_customers(self):

        time = self.current_vehicle[:, :, 3].clone()

        if self.init_customer_mask is None:
            reveal_dyn_reqs = torch.logical_and((self.customer_mask), (self.nodes[:, :, 3] <= time))
        else:
            reveal_dyn_reqs = torch.logical_and((self.customer_mask ^ self.init_customer_mask),
                                                (self.nodes[:, :, 3] <= time))

        if reveal_dyn_reqs.any():
            self.new_customer = True
            self.customer_mask = self.customer_mask ^ reveal_dyn_reqs
            self.mask = self.mask ^ reveal_dyn_reqs[:, None, :].expand(-1, self.vehicle_count, -1)
            self.vehicle_done = torch.logical_and(self.vehicle_done, (reveal_dyn_reqs.any(1) ^ True).unsqueeze(1))
            self.vehicles[:, :, 3] = torch.max(self.vehicles[:, :, 3], time)
            self._update_next_vehicle()

    def reset(self):
        # reset vehicle (minibatch*veh_count*veh_feature)
        self.vehicles = self.nodes.new_zeros((self.minibatch, self.vehicle_count, self.vehicle_feature))
        self.vehicles[:, :, :2] = self.nodes[:, :1, :2]
        self.vehicles[:, :, 2] = self.vehicle_time_budget

        # reset vehicle done
        self.vehicle_done = self.nodes.new_zeros((self.minibatch, self.vehicle_count), dtype=torch.bool)
        self.done = False

        # reset cust_mask
        self.customer_mask = self.nodes[:, :, 3] > 0
        if self.init_customer_mask is not None:
            self.customer_mask = self.customer_mask | self.init_customer_mask

        # reset new customers and served customer since now to zero (all false)
        self.new_customer = True
        self.served = torch.zeros_like(self.customer_mask)

        # reset mask (minibatch*veh_count*nodes)
        self.mask = self.customer_mask[:, None, :].repeat(1, self.vehicle_count, 1)

        # reset current vehicle index, current vehicle, current vehicle mask
        self.current_vehicle_index = self.nodes.new_zeros((self.minibatch, 1), dtype=torch.int64)

        self.current_vehicle = self.vehicles.gather(1,
                                                    self.current_vehicle_index[:, :, None].expand(-1, -1,
                                                                                                  self.vehicle_feature))
        self.current_vehicle_mask = self.mask.gather(1,
                                                     self.current_vehicle_index[:, :, None].expand(-1, -1,
                                                                                                   self.nodes_count))

    def step(self, customer_index, veh_index=None):
        dest = self.nodes.gather(1, customer_index[:, :, None].expand(-1, -1, self.customer_feature))
        dist, dyn_cust = self._update_current_vehicles(dest, customer_index)

        # cust = (dest[:, :, 3] >= 0).float()

        self._done(customer_index)
        self._update_mask(customer_index)
        self._update_next_vehicle(veh_index)

        # reward = -dist * (1 - dyn_cust*self.dynamic_reward)
        reward = self.current_vehicle[:, :, 7] - self.current_vehicle[:, :, 6] + self.dynamic_reward * dyn_cust
        pending_static_customers = torch.logical_and((self.served ^ True),
                                                     (self.nodes[:, :, 3] == 0)).float().sum(-1, keepdim=True) - 1

        reward -= self.pending_cost * pending_static_customers

        if self.done:

            if self.init_customer_mask is not None:
                self.served += self.init_customer_mask
            # penalty for pending customers
            pending_customers = torch.logical_and((self.served ^ True),
                                                  (self.nodes[:, :, 3] >= 0)).float().sum(-1, keepdim=True) - 1

            # TODO: penalty for having unused time budget as well not serving customers
            reward -= self.dynamic_reward * pending_customers

        self._update_dynamic_customers()

        return reward

    def state_dict(self, dest_dict=None):
        if dest_dict is None:
            dest_dict = {'vehicles': self.vehicles,
                         'vehicle_done': self.vehicle_done,
                         'served': self.served,
                         'mask': self.mask,
                         'current_vehicle_index': self.current_vehicle_index}

        else:
            dest_dict["vehicles"].copy_(self.vehicles)
            dest_dict["vehicle_done"].copy_(self.vehicle_done)
            dest_dict["served"].copy_(self.served)
            dest_dict["mask"].copy_(self.mask)
            dest_dict["current_vehicle_index"].copy_(self.current_vehicle_index)

        return dest_dict

    def load_state_dict(self, state_dict):
        self.vehicles.copy_(state_dict["vehicles"])
        self.vehicle_done.copy_(state_dict["vehicle_done"])
        self.served.copy_(state_dict["served"])
        self.mask.copy_(state_dict["mask"])
        self.current_vehicle_index.copy_(state_dict["current_vehicle_index"])

        self.current_vehicle = self.vehicles.gather(1,
                                                    self.current_vehicle_index[:, :, None].expand(-1, -1,
                                                                                                  self.vehicle_feature))
        self.current_vehicle_mask = self.mask.gather(1, self.current_vehicle_index[:, :, None].expand(-1, -1,
                                                                                                      self.customer_feature))

from __future__ import division, print_function
import numpy as np
import GPy
import matplotlib.pyplot as plt
from scipy.spatial.distance import cdist
import networkx as nx


class SafeMDP(object):
    def __init__(self, gp, world_shape, step_size, beta, altitudes, h, S0,
                 S_hat0, noise, L):

        self.gp = gp
        #    self.kernel = gp.kern
        #    self.likelihood = gp.likelihood
        self.altitudes = altitudes
        self.world_shape = world_shape
        self.step_size = step_size
        self.beta = beta
        self.noise = noise

        # Grids for the map
        self.ind, self.coord = self.grid()

        # Threshold
        self.h = h

        # Lipschitz
        self.L = L

        # Distances
        self.d = cdist(self.coord, self.coord)

        # Safe and expanders sets
        self.S = S0
        self.reach = np.empty_like(self.S, dtype=bool)
        self.ret = np.empty_like(self.S, dtype=bool)
        self.G = np.empty_like(self.S, dtype=bool)
        if np.isnan(S_hat0):
            self.S_hat = self.compute_S_hat0()
        else:
            self.S_hat = S_hat0

        # Set used to efficiently build the graph for the shortest path problem
        self.S_hat_old = np.zeros_like(self.S_hat, dtype=bool)

        # Target
        self.target_state = np.empty(2, dtype=int)
        self.target_action = np.empty(1, dtype=int)

        # Confidence intervals
        self.l = np.empty(self.S.shape, dtype=float)
        self.u = np.empty(self.S.shape, dtype=float)
        self.l[:] = -np.inf
        self.u[:] = np.inf
        self.l[self.S] = h

        # True sets
        self.true_S = self.compute_true_safe_set()
        self.true_S_hat = self.compute_true_S_hat()

        # Graph for shortest path
        # self.graph = nx.DiGraph()
        self.graph_lazy = nx.DiGraph()
        self.compute_graph_lazy()

    def grid(self):
        """
        Creates grids of coordinates and indices of state space

        Returns
        -------
        states_ind: np.array
            (n*m) x 2 array containing the indices of the states
        states_coord: np.array
            (n*m) x 2 array containing the coordinates of the states
        """
        # Create grid of indices
        n, m = self.world_shape
        xx, yy = np.meshgrid(np.arange(n), np.arange(m), indexing="ij")
        states_ind = np.vstack((xx.flatten(), yy.flatten())).T
        # Grid of coordinates (used to compute Gram matrix)
        step1, step2 = self.step_size
        xx, yy = np.meshgrid(np.linspace(0, (n - 1) * step1, n),
                             np.linspace(0, (m - 1) * step2, m),
                             indexing="ij")
        states_coord = np.vstack((xx.flatten(), yy.flatten())).T
        return states_ind, states_coord

    def update_confidence_interval(self):
        """
        Updates the lower and the upper bound of the confidence intervals
        using then posterior distribution over the gradients of the altitudes

        Returns
        -------
        l: np.array
            lower bound of the safety feature (mean - beta*std)
        u: np.array
            upper bound of the safety feature (mean - beta*std)
        """
        # Predict safety feature
        mu, s = self.gp.predict_jacobian(self.coord, full_cov=False)
        mu = np.squeeze(mu)

        # Initialize mean and variance over abstract MDP
        mu_abstract = np.zeros(self.S.shape)
        s_abstract = np.copy(mu_abstract)

        # Safety features for real states s
        mu_abstract[:, 0] = self.h
        s_abstract[:, 0] = 0

        # Safety feature for (s,a) pairs
        mu_abstract[:, 1] = -mu[:, 1]
        mu_abstract[:, 3] = mu[:, 1]
        s_abstract[:, 1] = s[:, 1]
        s_abstract[:, 3] = s[:, 1]
        mu_abstract[:, 2] = -mu[:, 0]
        mu_abstract[:, 4] = mu[:, 0]
        s_abstract[:, 2] = s[:, 0]
        s_abstract[:, 4] = s[:, 0]
        # Lower and upper bound of confidence interval
        self.l = mu_abstract - self.beta * np.sqrt(s_abstract)
        self.u = mu_abstract + self.beta * np.sqrt(s_abstract)

    def boolean_dynamics(self, bool_mat, action):
        """
        Given a boolean array over the state space, it shifts all the boolean
        values according to the dynamics of the system using the action
        provided as input. For example, if true entries of bool_mat indicate
        the safe states, boolean dynamics returns an array whose true entries
        indicate states that can be reached from the safe set with action =
        action

        Parameters
        ----------
        bool_mat: np.array
            n_states x 1 array of booleans indicating which initial states
            satisfy a given property
        action: int
            action we want to compute the dynamics with

        Returns
        -------
        return: np.array
            n_states x 1 array of booleans. If the entry in boolean_mat in
            input is equal to true for a state s, the output will have then
            entry corresponding to f(s, action) set to true (f represents the
            dynamics of the system)
        """
        start = bool_mat.reshape(self.world_shape).copy()
        end = bool_mat.reshape(self.world_shape).copy()

        if action == 1:  # moves right by one column
            end[:, 1:] = start[:, 0:-1]
            end[:, -1] = np.logical_or(end[:, -1], start[:, -1])
            end[:, 0] = False

        elif action == 2:  # moves down by one row
            end[1:, :] = start[0:-1, :]
            end[-1, :] = np.logical_or(end[-1, :], start[-1, :])
            end[0, :] = False

        elif action == 3:  # moves left by one column
            end[:, 0:-1] = start[:, 1:]
            end[:, 0] = np.logical_or(end[:, 0], start[:, 0])
            end[:, -1] = False

        elif action == 4:  # moves up by one row
            end[0:-1, :] = start[1:, :]
            end[0, :] = np.logical_or(end[0, :], start[0, :])
            end[-1, :] = False

        else:
            raise ValueError("Unknown action")
        return np.reshape(end, (np.prod(self.world_shape)))

    def r_reach(self):
        """
        computes the union of the points in self.reach and the points that are
        reachable in one time step from self.reach and that are above safety
        threshold

        Returns
        -------
        changed: bool
            Indicates whether self.reach and the newly computed set are
            different or not
        """

        # Initialize
        reachable_from_reach = np.zeros(self.S.shape, dtype=bool)

        # From s to (s,a) pair
        reachable_from_reach[self.reach[:, 0], 1:] =\
            self.S[self.reach[:, 0], 1:]

        # From (s,a) to s
        for action in range(1, self.S.shape[1]):
            tmp = self.boolean_dynamics(self.reach[:, action], action)
            reachable_from_reach[:, 0] = np.logical_or(
                reachable_from_reach[:, 0], tmp)
        reachable_from_reach[:, 0] = np.logical_and(reachable_from_reach[:, 0],
                                                    self.S[:, 0])
        reachable_from_reach = np.logical_or(reachable_from_reach, self.reach)
        changed = not np.all(self.reach == reachable_from_reach)
        self.reach[:] = reachable_from_reach
        return changed

    def boolean_inverse_dynamics(self, bool_mat, action):
        """
        Similar to boolean dynamics. The difference is that here the
        boolean_mat input indicates the arrival states that satisfy a given
        property and the function returns the initial states from which the
        arrival state can be reached applying the action input.

        Parameters
        ----------
        bool_mat: np.array
                  n_states x 1 array of booleans indicating which arrival
                  states satisfy a given property
        action: int
                action we want to compute the inverse dynamics with

        Returns
        -------
        return: np.array
                n_states x 1 array of booleans. If the entry in the output
                is set to true for a state s, the input boolean_mat has the
                entry corresponding to f(s, action) equal to true
                (f represents the dynamics of the system)
        """
        start = bool_mat.reshape(self.world_shape).copy()
        end = bool_mat.reshape(self.world_shape).copy()

        if action == 3:  # moves right by one column
            start[:, 1:] = end[:, 0:-1]
            start[:, 0] = end[:, 0]

        elif action == 4:  # moves down by one row
            start[1:, :] = end[0:-1, :]
            start[0, :] = end[0, :]

        elif action == 1:  # moves left by one column
            start[:, 0:-1] = end[:, 1:]
            start[:, -1] = end[:, -1]

        elif action == 2:  # moves up by one row
            start[0:-1, :] = end[1:, :]
            start[-1, :] = end[-1, :]

        else:
            raise ValueError("Unknown action")
        return np.reshape(start, (np.prod(self.world_shape)))

    def r_ret(self):
        """
        computes the union of the points in self.ret and the points from which
        it is possible to recover to self.ret and that are above safety
        threshold

        Returns
        -------
        changed: bool
            Indicates whether self.ret and the newly computed set are
            different or not
        """

        # Initialize
        recover_to_ret = np.zeros(self.S.shape, dtype=bool)

        # From s in S to (s,a) in ret
        recover_to_ret[self.S[:, 0], 0] = np.any(
            np.logical_and(self.S[self.S[:, 0], 1:],
                           self.ret[self.S[:, 0], 1:]), axis=1)

        # From (s,a) in S to s in ret
        for action in range(1, self.S.shape[1]):
            tmp = self.boolean_inverse_dynamics(self.ret[:, 0], action)
            recover_to_ret[:, action] = np.logical_and(tmp, self.S[:, action])
        recover_to_ret = np.logical_or(recover_to_ret, self.ret)
        changed = not np.all(self.ret == recover_to_ret)
        self.ret[:] = recover_to_ret
        return changed

    def compute_expanders(self):
        self.G[:] = False
        states_ind = np.arange(self.S_hat.shape[0])
        for action in range(1, self.S_hat.shape[1]):

            # Extract distance from safe points to non safe ones
            dist_tmp = self.d[np.ix_(self.S_hat[:, action],
                                     np.logical_not(self.S[:, action]))]

            # Find states for which (s, action) is in S_hat
            non_zeros = states_ind[self.S_hat[:, action]]

            # Check condition for expanders
            expanders = non_zeros[np.any(self.u[self.S_hat[:, action],
                                         action:action + 1] - self.L *
                                         dist_tmp >= self.h,
                                         axis=1)]
            if expanders.size != 0:
                self.G[expanders, action] = True

    def update_sets(self):
        """
        Updates the sets S, S_hat and G taking with the available observation
        """
        self.update_confidence_interval()
        self.S = self.l >= self.h

        # Actions that takes agent out of boundaries are assumed to be unsafe
        n, m = self.world_shape
        self.S[m - 1:m * (n + 1) - 1:m, 1] = False
        self.S[(n - 1) * m:n * m, 2] = False
        self.S[0:n * m:m, 3] = False
        self.S[0:m, 4] = False

        self.reach[:] = self.S_hat
        self.ret[:] = self.S_hat

        while self.r_reach():
            pass
        while self.r_ret():
            pass
        self.S_hat_old[:] = self.S_hat
        self.S_hat[:] = np.logical_or(self.S_hat,
                                      np.logical_and(self.reach, self.ret))

        self.compute_expanders()

    def plot_S(self, S):
        """
        Plot the set of safe states

        Parameters
        ----------
        S: np.array(dtype=bool)
            n_states x (n_actions + 1) array of boolean values that indicates
            the safe set

        """
        for action in range(1):
            plt.figure(action)
            plt.imshow(np.reshape(S[:, action], self.world_shape).T,
                       origin="lower", interpolation="nearest")
            plt.title("action " + str(action))
        plt.show()

    def add_observation(self, state_mat_ind, action):
        """
        Adds an observation of the given state-action pair. Observing the pair
        (s, a) means to add an observation of the altitude at s and an
        observation of the altitude at f(s, a)

        Parameters
        ----------
        state_mat_ind: np.array
            i,j indexing of the state of the target state action pair
        action: int
            action of the target state action pair
        """

        # Observation of previous state
        state_vec_ind = mat2vec(state_mat_ind, self.world_shape)
        obs_state = self.altitudes[state_vec_ind]
        tmpX = np.vstack((self.gp.X,
                          self.coord[state_vec_ind, :].reshape(1, 2)))
        tmpY = np.vstack((self.gp.Y, obs_state))

        # Observation of next state
        next_state_mat_ind = self.dynamics(state_mat_ind, action)
        next_state_vec_ind = mat2vec(next_state_mat_ind, self.world_shape)
        obs_next_state = self.altitudes[next_state_vec_ind]

        # Update observations
        tmpX = np.vstack((tmpX,
                          self.coord[next_state_vec_ind, :].reshape(1, 2)))
        tmpY = np.vstack((tmpY, obs_next_state))
        self.gp.set_XY(tmpX, tmpY)

    def target_sample(self):
        """
        Compute the next target (s, a) to sample (highest uncertainty within
        G or S_hat)
        """
        if np.any(self.G):
            # Extract elements in G
            non_z = np.nonzero(self.G)

            # Compute uncertainty
            w = self.u[self.G] - self.l[self.G]

            # Find   max uncertainty
            ind = np.argmax(w)

        else:
            # Extract elements in S_hat
            non_z = np.nonzero(self.S_hat)

            # Compute uncertainty
            w = self.u[self.S_hat] - self.l[self.S_hat]

            # Find   max uncertainty
            ind = np.argmax(w)

        state = non_z[0][ind]
        # Store (s, a) pair
        self.target_state[:] = vec2mat(state, self.world_shape)
        self.target_action = non_z[1][ind]

    def dynamics(self, states, action):
        """
        Dynamics of the system
        The function computes the one time step dynamic evolution of the system
        for any number of initial state and for one given action

        Parameters
        ----------
        states: np.array
            Two dimensional array. Each row contains the (x,y) coordinates of
            the starting points we want to compute the evolution for
        action: int
            Control action (1 = up, 2 = right, 3 = down, 4 = left)

        Returns
        -------
        next_states: np.array
            Two dimensional array. Each row contains the (x,y) coordinates
            of the state that results from applying action to the corresponding
            row of the input states
        """
        n, m = self.world_shape
        if states.ndim == 1:
            states = states.reshape(1, 2)
        next_states = np.copy(states)
        if action == 1:
            next_states[:, 1] += 1
            next_states[next_states[:, 1] > m - 1, 1] = m - 1
        elif action == 2:
            next_states[:, 0] += 1
            next_states[next_states[:, 0] > n - 1, 0] = n - 1
        elif action == 3:
            next_states[:, 1] -= 1
            next_states[next_states[:, 1] < 0, 1] = 0
        elif action == 4:
            next_states[:, 0] -= 1
            next_states[next_states[:, 0] < 0, 0] = 0
        else:
            raise ValueError("Unknown action")
        return next_states

    def compute_true_safe_set(self):
        """
        Computes the safe set given a perfect knowledge of the map

        Returns
        -------
        true_safe: np.array
            Boolean array n_states x (n_actions + 1).
        """

        # Initialize
        true_safe = np.empty_like(self.S, dtype=bool)

        # All true states are safe
        true_safe[:, 0] = True

        # Compute safe (s, a) pairs
        for action in range(1, self.S.shape[1]):
            next_mat_ind = self.dynamics(self.ind, action)
            next_vec_ind = mat2vec(next_mat_ind, self.world_shape)
            true_safe[:, action] = ((self.altitudes -
                                     self.altitudes[next_vec_ind]) /
                                    self.step_size[0]) >= self.h

        # (s, a) pairs that lead out of boundaries are not safe
        n, m = self.world_shape
        true_safe[m - 1:m * (n + 1) - 1:m, 1] = False
        true_safe[(n - 1) * m:n * m, 2] = False
        true_safe[0:n * m:m, 3] = False
        true_safe[0:m, 4] = False
        return true_safe

    def compute_true_S_hat(self):
        """
        Computes the safe set with reachability and recovery properties
        given a perfect knowledge of the map

        Returns
        -------
        true_safe: np.array
            Boolean array n_states x (n_actions + 1).
        """
        # Initialize
        true_S_hat = np.zeros_like(self.S, dtype=bool)
        self.reach[:] = self.S_hat
        self.ret[:] = self.S_hat

        # Substitute S with true S for r_reach and r_ret methods
        tmp = np.copy(self.S)
        self.S[:] = self.true_S

        # Reachable and recovery set
        while self.r_reach():
            pass
        while self.r_ret():
            pass

        # Points are either in S_hat or in the intersection of reachable and
        #  recovery sets
        true_S_hat[:] = np.logical_or(self.S_hat,
                                      np.logical_and(self.ret, self.reach))

        # Reset value of S
        self.S[:] = tmp
        return true_S_hat

    def compute_S_hat0(self):
        """
        Compute a random initial safe seed. WARNING:  at the moment actions
        for returning are not included

        Returns
        ------
        S_hat: np.array
            Boolean array n_states x (n_actions + 1).
        """
        # Initialize
        safe = np.zeros(self.S.shape[1] - 1, dtype=bool)
        S_hat = np.zeros_like(self.S, dtype=bool)

        # Loop until you find a valid initial seed
        while not np.any(safe):
            # Pick random state
            s = np.random.choice(self.ind.shape[0])

            # Compute next state for every action and check safety of (s, a)
            # pair
            s_next = np.empty(self.S.shape[1] - 1, dtype=int)
            for action in range(1, self.S.shape[1]):

                s_next[action - 1] = mat2vec(
                    self.dynamics(self.ind[s, :], action),
                    self.world_shape).astype(int)
                alt = self.altitudes[s]
                alt_next = self.altitudes[s_next[action - 1]]

                if s != (s_next[action - 1] and (alt - alt_next) /
                         self.step_size[0] >= self.h):
                    safe[action - 1] = True
        # Set initial state, (s, a) pairs and arrival state as safe
        s_next = s_next[safe]
        S_hat[s, 0] = True
        S_hat[s_next, 0] = True
        S_hat[s, 1:] = safe
        return S_hat

    def dynamics_vec_ind(self, states_vec_ind, action):
        """
        Dynamic evolution of the system defined in vector representation of
        the states

        Parameters
        ----------
        states_vec_ind: np.array
            Contains all the vector indexes of the states we want to compute
            the dynamic evolution for
        action: int
            action performed by the agent

        Returns
        -------
        next_states_vec_ind: np.array
            vector index of states resulting from applying the action given
            as input to the array of starting points given as input
        """
        n, m = self.world_shape
        next_states_vec_ind = np.copy(states_vec_ind)
        if action == 1:
            next_states_vec_ind[:] = states_vec_ind + 1
            condition = np.mod(next_states_vec_ind, m) == 0
            next_states_vec_ind[condition] = states_vec_ind[condition]
        elif action == 2:
            next_states_vec_ind[:] = states_vec_ind + m
            condition = next_states_vec_ind >= m * n
            next_states_vec_ind[condition] = states_vec_ind[condition]
        elif action == 3:
            next_states_vec_ind[:] = states_vec_ind - 1
            condition = np.mod(states_vec_ind, m) == 0
            next_states_vec_ind[condition] = states_vec_ind[condition]
        elif action == 4:
            next_states_vec_ind[:] = states_vec_ind - m
            condition = next_states_vec_ind <= -1
            next_states_vec_ind[condition] = states_vec_ind[condition]
        else:
            raise ValueError("Unknown action")
        return next_states_vec_ind

    # def compute_graph(self):
    #     states_vec_ind = np.arange(self.S_hat.shape[0])
    #
    #     for action in range(1, self.S_hat.shape[1]):
    #
    #         # States where is safe to apply action = action
    #         safe_states_vec_ind = states_vec_ind[self.S_hat[:, action]]
    #
    #         # Resulting states when applying action at safe_states_vec_ind
    #         next_states_vec_ind = self.dynamics_vec_ind(
    # safe_states_vec_ind, action)
    #
    #         # Resulting states that are also safe
    #         condition = self.S_hat[next_states_vec_ind, 0]
    #
    #         # Add edges to graph
    #         start = self.ind[safe_states_vec_ind[condition], :]
    #         end = self.ind[next_states_vec_ind[condition], :]
    #         self.graph.add_edges_from(zip(map(tuple, start), map(tuple,
    # end)))

    def compute_graph_lazy(self):
        states_vec_ind = np.arange(self.S_hat.shape[0])

        for action in range(1, self.S_hat.shape[1]):
            # States where is safe to apply action = action
            safe_states_vec_ind = states_vec_ind[
                np.logical_and(self.S_hat[:, action],
                               np.logical_not(self.S_hat_old[:, action]))]

            # Resulting states when applying action at safe_states_vec_ind
            next_states_vec_ind = self.dynamics_vec_ind(safe_states_vec_ind,
                                                        action)

            # Resulting states that are also safe
            condition = self.S_hat[next_states_vec_ind, 0]

            # Add edges to graph
            start = self.ind[safe_states_vec_ind[condition], :]
            end = self.ind[next_states_vec_ind[condition], :]
            self.graph_lazy.add_edges_from(zip(map(tuple, start),
                                               map(tuple, end)))


def vec2mat(vec_ind, world_shape):
    """
    Converts from vector indexing to matrix indexing

    Parameters
    ----------
    vec_ind: np.array
        Each element contains the vector indexing of a state we want to do
        the convesrion for
    world_shape: shape
        Tuple that contains the shape of the grid world n x m

    Returns
    -------
    return: np.array
        ith row contains the (x,y) coordinates of the ith element of the
        input vector vec_ind
    """
    n, m = world_shape
    row = np.floor(vec_ind / m)
    col = np.mod(vec_ind, m)
    return np.array([row, col]).astype(int)


def mat2vec(states_mat_ind, world_shape):
    """
    Converts from matrix indexing to vector indexing

    Parameters
    ----------
    states_mat_ind: np.array
        Each row contains the (x,y) coordinates of each state we want to do
        the conversion for
    world_shape: shape
        Tuple that contains the shape of the grid world n x m

    Returns
    -------
    vec_ind: np.array
        Each element contains the vector indexing of the point in the
        corresponding row of the input states_mat_ind
    """
    if states_mat_ind.ndim == 1:
        states_mat_ind = states_mat_ind.reshape(1, 2)
    m = world_shape[1]
    vec_ind = states_mat_ind[:, 1] + states_mat_ind[:, 0] * m
    return vec_ind.astype(int)


def draw_gp_sample(kernel, world_shape, step_size):
    """
    Draws a sample from a Gaussian process distribution over a user
    specified grid

    Parameters
    ----------
    kernel: GPy kernel
        Defines the GP we draw a sample from
    world_shape: tuple
        Shape of the grid we use for sampling
    step_size: tuple
        Step size along any axis to find linearly spaced points
    """
    # Compute linearly spaced grid
    n, m = world_shape
    step1, step2 = step_size
    xx, yy = np.meshgrid(np.linspace(0, (n - 1) * step1, n),
                         np.linspace(0, (m - 1) * step2, m),
                         indexing="ij")
    coord = np.vstack((xx.flatten(), yy.flatten())).T

    # Draw a sample from GP
    cov = kernel.K(coord)
    sample = np.random.multivariate_normal(np.zeros(coord.shape[0]), cov)
    return sample, coord


def manhattan_dist(a, b):
    (x1, y1) = a
    (x2, y2) = b
    return np.fabs(x1 - x2) + np.fabs(y1 - y2)


# test
if __name__ == "__main__":
    import time

    mars = False

    if mars:
        from osgeo import gdal

        # Extract and plot Mars data
        world_shape = (60, 60)
        step_size = (1., 1.)
        gdal.UseExceptions()
        ds = gdal.Open(
            "/Users/matteoturchetta/PycharmProjects/SafeMDP/src/mars.tif")
        band = ds.GetRasterBand(1)
        elevation = band.ReadAsArray()
        startX = 11370
        startY = 3110
        altitudes = np.copy(elevation[startX:startX + world_shape[0],
                            startY:startY + world_shape[1]])
        mean_val = (np.max(altitudes) + np.min(altitudes)) / 2.
        altitudes[:] = altitudes - mean_val

        plt.imshow(altitudes.T, origin="lower", interpolation="nearest")
        plt.colorbar()
        plt.show()
        altitudes = altitudes.flatten()

        # Define coordinates
        n, m = world_shape
        step1, step2 = step_size
        xx, yy = np.meshgrid(np.linspace(0, (n - 1) * step1, n),
                             np.linspace(0, (m - 1) * step2, m), indexing="ij")
        coord = np.vstack((xx.flatten(), yy.flatten())).T

        # Safety threshold
        h = -np.tan(np.pi / 6.)

        # Lipschitz
        L = 1.

        # Scaling factor for confidence interval
        beta = 2

        # Initialize safe sets
        S0 = np.zeros((np.prod(world_shape), 5), dtype=bool)
        S0[:, 0] = True
        S_hat0 = np.nan

        # Initialize for performance
        lengthScale = np.linspace(6.5, 7., num=2)
        size_true_S_hat = np.empty_like(lengthScale, dtype=int)
        size_S_hat = np.empty_like(lengthScale, dtype=int)
        true_S_hat_minus_S_hat = np.empty_like(lengthScale, dtype=int)
        S_hat_minus_true_S_hat = np.empty_like(lengthScale, dtype=int)

        # Initialize data for GP
        n_samples = 1
        ind = np.random.choice(range(altitudes.size), n_samples)
        X = coord[ind, :]
        Y = altitudes[ind].reshape(n_samples, 1) + np.random.randn(n_samples,
                                                                   1)

        for index, length in enumerate(lengthScale):

            # Define and initialize GP
            noise = 0.04
            kernel = GPy.kern.RBF(input_dim=2, lengthscale=length,
                                  variance=121.)
            lik = GPy.likelihoods.Gaussian(variance=noise ** 2)
            gp = GPy.core.GP(X, Y, kernel, lik)

            # Define SafeMDP object
            x = SafeMDP(gp, world_shape, step_size, beta, altitudes, h, S0,
                        S_hat0, noise, L)

            # Insert samples from (s, a) in S_hat0
            tmp = np.arange(x.ind.shape[0])
            s_vec_ind = tmp[np.any(x.S_hat[:, 1:], axis=1)]
            state = vec2mat(s_vec_ind, x.world_shape).T
            tmp = np.arange(1, x.S.shape[1])
            actions = tmp[x.S_hat[s_vec_ind, 1:].squeeze()]
            for i in range(1):
                x.add_observation(state, np.random.choice(actions))

            # Remove samples used for GP initialization and possibly
            # hyperparameters optimization
            x.gp.set_XY(x.gp.X[n_samples:, :], x.gp.Y[n_samples:])

            t = time.time()
            for i in range(50):
                x.update_sets()
                x.target_sample()
                x.add_observation(x.target_state, x.target_action)
                # print (x.target_state, x.target_action)
                # print(i)
                print(np.any(x.G))
            print(str(time.time() - t) + "seconds elapsed")

            # Plot safe sets
            x.plot_S(x.S_hat)
            x.plot_S(x.true_S_hat)

            # Print and store performance
            print(np.sum(np.logical_and(x.true_S_hat,
                                        np.logical_not(x.S_hat))))
            # in true S_hat and not S_hat
            print(np.sum(np.logical_and(x.S_hat,
                                        np.logical_not(x.true_S_hat))))
            # in S_hat and not true S_hat
            size_S_hat[index] = np.sum(x.S_hat)
            size_true_S_hat[index] = np.sum(x.true_S_hat)
            true_S_hat_minus_S_hat[index] = np.sum(
                np.logical_and(x.true_S_hat, np.logical_not(x.S_hat)))
            S_hat_minus_true_S_hat[index] = np.sum(
                np.logical_and(x.S_hat, np.logical_not(x.true_S_hat)))

    else:
        # Define world
        world_shape = (40, 40)
        step_size = (0.5, 0.5)

        # Define GP
        noise = 0.001
        kernel = GPy.kern.RBF(input_dim=2, lengthscale=(2., 2.), variance=1.,
                              ARD=True)
        lik = GPy.likelihoods.Gaussian(variance=noise ** 2)
        lik.constrain_bounded(1e-6, 10000.)

        # Sample and plot world
        altitudes, coord = draw_gp_sample(kernel, world_shape, step_size)
        fig = plt.figure()
        ax = fig.add_subplot(111, projection='3d')
        ax.plot_trisurf(coord[:, 0], coord[:, 1], altitudes)
        plt.show()

        # Define coordinates
        n, m = world_shape
        step1, step2 = step_size
        xx, yy = np.meshgrid(np.linspace(0, (n - 1) * step1, n),
                             np.linspace(0, (m - 1) * step2, m),
                             indexing="ij")
        coord = np.vstack((xx.flatten(), yy.flatten())).T

        # Safety threhsold
        h = -0.9

        # Lipschitz
        L = 0

        # Scaling factor for confidence interval
        beta = 2

        # Data to initialize GP
        n_samples = 1
        ind = np.random.choice(range(altitudes.size), n_samples)
        X = coord[ind, :]
        Y = altitudes[ind].reshape(n_samples, 1) + np.random.randn(n_samples,
                                                                   1)
        gp = GPy.core.GP(X, Y, kernel, lik)

        # Initialize safe sets
        S0 = np.zeros((np.prod(world_shape), 5), dtype=bool)
        S0[:, 0] = True
        S_hat0 = np.nan

        # Define SafeMDP object
        x = SafeMDP(gp, world_shape, step_size, beta, altitudes, h, S0, S_hat0,
                    noise, L)

        # Insert samples from (s, a) in S_hat0
        tmp = np.arange(x.ind.shape[0])
        s_vec_ind = tmp[np.any(x.S_hat[:, 1:], axis=1)]
        state = vec2mat(s_vec_ind, x.world_shape).T
        tmp = np.arange(1, x.S.shape[1])
        actions = tmp[x.S_hat[s_vec_ind, 1:].squeeze()]
        for i in range(1):
            x.add_observation(state, np.random.choice(actions))

        # Remove samples used for GP initialization
        x.gp.set_XY(x.gp.X[n_samples:, :], x.gp.Y[n_samples:])

        t = time.time()
        for i in range(100):
            x.update_sets()
            x.target_sample()
            x.add_observation(x.target_state, x.target_action)
            x.compute_graph_lazy()
            # plt.figure(1)
            # plt.clf()
            # nx.draw_networkx(x.graph)
            # plt.show()
            print("Iteration:   " + str(i))

        print(str(time.time() - t) + "seconds elapsed")

        # Plot safe sets
        x.plot_S(x.S_hat)
        x.plot_S(x.true_S_hat)

        # Classification performance
        print(np.sum(np.logical_and(x.true_S_hat, np.logical_not(
            x.S_hat))))  # in true S_hat and not S_hat
        print(np.sum(np.logical_and(x.S_hat, np.logical_not(x.true_S_hat))))
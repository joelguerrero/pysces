from __future__ import division

import numpy as np

__all__ = ['BoundVortexPanels', 'FreeVortexParticles', 'SourceDoubletPanels']

class VortexPanels(object):
    pass

class LumpedVortex(object):
    """A base class for schemes based on vortex particles"""
    _core_radius = 1.e-3

    @property
    def core_radius(self):
        r"""Radius to use for regularization of the induced velocity

        For points closer than ``core_radius``, the vortex is
        treated as inducing solid-body rotation.

        See also
        --------
        induced_velocity_single

        """
        return self._core_radius

    @core_radius.setter
    def core_radius(self, value):
        self._core_radius = value

    def induced_velocity_single(self, x, xvort, gam):
        r"""Compute velocity induced at points x by a single vortex

        Parameters
        ----------
        x : 2d array
            Locations at which to compute induced velocity.  Expressed as
            column vectors (i.e., shape should be (2,n))
        xvort : 1d array
            Location of vortex (shape should be (2,1))
        gam : float
            Strength of vortex

        Notes
        -----
        Induced velocity is

        .. math:: u_\theta = -\frac{\Gamma}{2 \pi r}

        where r is the distance between the point and the vortex.  If this
        distance is less than :class:`core_radius` :math:`r_0`, the velocity is
        regularized as solid-body rotation, with

        .. math:: u_\theta = -\frac{\Gamma r}{2\pi r_0^2}`
        """
        r = x - xvort[:,np.newaxis]
        rsq = np.maximum(np.sum(r * r, 0), self.core_radius**2)
        # alternative regularization (Krasny, Eldredge)
        # rsq = np.sum(r * r, 0) + self._core_radius**2
        vel = gam / (2 * np.pi) * np.vstack([r[1], -r[0]]) / rsq
        return vel

class BoundVortexPanels(LumpedVortex):
    """A class for bound vortex panels"""

    def __init__(self, body, Uinfty=(1,0)):
        self._body = body
        self._update(Uinfty)

    def _update(self, Uinfty=(1,0)):
        # here, Uinfty is used solely to determine direction of panel, for
        # placing colllocation points and vortex positions
        q = self._body.get_points(body_frame=True)
        dq = np.diff(q)
        self._numpanels = dq.shape[1]
        self._normals = (np.vstack([dq[1,:], -dq[0,:]]) /
                         np.linalg.norm(dq, axis=0))
        q25 = q[:,:-1] + 0.25 * dq
        q75 = q[:,:-1] + 0.75 * dq
        # vortex positions at 1/4 chord of panel
        # collocation points at 3/4 chord of panel
        # Determine orientation from Uinfty
        Uinfty = np.array(Uinfty)
        self._xvort = q25.copy()
        self._xcoll = q75.copy()
        # reverse direction where dq is against flow direction
        top, = np.where(np.dot(Uinfty, dq) <= 0)
        self._xvort[:,top] = q75[:,top]
        self._xcoll[:,top] = q25[:,top]
        # find trailing edge and wake vortex direction
        if np.linalg.norm(q[:,0] - q[:,-1]) < 0.005:
            # closed body
            self._trailing_edge = 0.5 * (q[:,0] + q[:,-1])
            wake_dir = -0.5 * (dq[:,0] - dq[:,-1])
            self._wake_dir = wake_dir / np.linalg.norm(wake_dir)
        else:
            # thin airfoil
            self._trailing_edge = q[:,0]
            self._wake_dir = -dq[:,0] / np.linalg.norm(dq[:,0])
        self._gam = np.zeros(self._numpanels)
        self._influence_matrix = None

    def update_positions(self):
        # If non-rigid bodies are used, update panel positions here.
        #
        # Note that if the only motion is rigid body motion, the panel positions
        # do not need to be updated, since they are in body-fixed frame

        # need to recompute influence matrix when points change
        self._influence_matrix = None

    @property
    def influence_matrix(self):
        if self._influence_matrix is None:
            # time to recompute
            n = self._numpanels
            A = np.zeros((n, n))
            for i, xv in enumerate(np.transpose(self._xvort)):
                vel = self.induced_velocity_single(self._xcoll, xv, 1)
                A[:, i] = np.sum(vel * self._normals, 0)
            self._influence_matrix = A
        return self._influence_matrix

    def induced_velocity(self, x):
        """Compute the induced velocity at the given point(s)

        Parameters
        ----------
        x : 2d array
            Locations at which to compute induced velocity.  Expressed as
            column vectors (i.e., shape should be (2,n))

        See also
        --------
        induced_velocity_single

        """
        # transform vortex positions to inertial frame
        motion = self._body.get_motion()
        xvort_in = motion.map_position(self._xvort) if motion else self._xvort
        vel = np.zeros_like(x)
        # TODO: write test exposing the following bug (computing wrt body frame)
        # for xv, gam in zip(np.transpose(self._xvort), self._gam):
        for xv, gam in zip(np.transpose(xvort_in), self._gam):
            vel += self.induced_velocity_single(x, xv, gam)
        return vel

    def update_strengths(self, Uinfty=(1,0)):
        """Update vortex strengths"""
        rhs = self.compute_rhs(Uinfty)
        self._gam = np.linalg.solve(self.influence_matrix, rhs)

    def update_strengths_unsteady(self, dt, Uinfty=(1,0), wake=None, circ=None,
                                  wake_fac=0.25):
        """Update strengths for unsteady calculation

        Shed a new wake panel (not added into wake)

        Parameters
        ----------
        dt : float
            Timestep
        Uinfty : array_like, optional
            Farfield fluid velocity (default (1,0))
        wake : wake panel object, optional
            Induces velocities on the body (default None)
        circ : float, optional
            Total bound circulation, for enforcing Kelvin's circulation theorem.
            If None (default), obtain the total circulation from the wake,
            assuming overall circulation (body + wake) is zero
        wake_fac : float, optional
            New wake vortex is placed a distance wake_fac * Uinfty * dt from
            trailing edge (see Katz & Plotkin, p390).
        """

        # determine new wake vortex position (in body-fixed frame)
        distance = wake_fac * np.sqrt(Uinfty[0]**2 + Uinfty[1]**2) * dt
        x_shed = self._trailing_edge + distance * self._wake_dir
        # compute velocity induced on collocation points by newly shed vortex
        # (done in the body-fixed frame)
        shed_vel = self.induced_velocity_single(self._xcoll, x_shed, 1)
        shed_normal = np.sum(shed_vel * self._normals, 0)
        # determine overall influence matrix, including newly shed vortex
        # last equation: sum of all the vortex strengths = total circulation
        A = np.vstack([np.hstack([self.influence_matrix,
                                  shed_normal[:,np.newaxis]]),
                       np.ones((1, self._numpanels + 1))])

        rhs0 = self.compute_rhs(Uinfty, wake)
        if circ is None:
            if wake is None:
                circ = 0
            else:
                circ = -wake.circulation
        rhs = np.hstack([rhs0, circ])

        gam = np.linalg.solve(A, rhs)
        self._gam = gam[:-1]
        self._x_shed = x_shed
        self._gam_shed = gam[-1]

    def compute_rhs(self, Uinfty=(1,0), wake=None):
        # get collocation points and normals
        motion = self._body.get_motion()
        if motion:
            xcoll_inertial = motion.map_position(self._xcoll)
            normals_inertial = motion.map_vector(self._normals)
        else:
            xcoll_inertial = self._xcoll
            normals_inertial = self._normals
        # velocity induced by wake
        if wake:
            vel = wake.induced_velocity(xcoll_inertial)
        else:
            vel = np.zeros((2,self._numpanels))
        # assume body is not deforming: only motion is translation/rotation
        if motion:
            vel -= motion.map_velocity(self._xcoll)
        vel += np.array(Uinfty)[:,np.newaxis]
        # compute -v . n
        return -np.sum(vel * normals_inertial, 0)

    def get_newly_shed(self):
        """Return newly shed wake vortex in the inertial frame

        Returns
        -------
        x_shed : 1d array, shape (2,)
            Location of newly shed wake vortex, in inertial frame
        gam_shed : float
            Strength of newly shed vortex
        """
        motion = self._body.get_motion()
        if motion:
            x_shed_inertial = motion.map_position(self._x_shed)
        else:
            x_shed_inertial = np.array(self._x_shed, copy=True)
        return x_shed_inertial, self._gam_shed

    @property
    def vortices(self):
        return self._xvort, self._gam

    @property
    def collocation_pts(self):
        return self._xcoll

    @property
    def normals(self):
        return self._normals[0,:], self._normals[1,:]

class FreeVortexParticles(LumpedVortex):
    def __init__(self):
        self.reset()

    def reset(self):
        self._x_vortices = None
        self._gamma = list()
        self._num_vortices = 0
        self._circulation = 0

    @property
    def circulation(self):
        """Total circulation of the vortex particles"""
        return self._circulation

    def advect(self, dt, Uinfty=(0,0), body=None):
        """Advect the vortex particles forward one step in time

        Parameters
        ----------
        dt : float
            Timestep
        Uinfty : array_like, optional
            Farfield velocity, default (0,0)
        body : vortex panel object (optional)
            Optional body also contributing to induced velocity of the particles

        Notes
        -----
        An explicit Euler update is used, where the particle positions are
        incremented by ``vel * dt``, where ``vel`` is the induced velocity

        """
        # explicit Euler update
        vel = self.induced_velocity(self._x_vortices)
        if body:
            vel += body.induced_velocity(self._x_vortices)
        if Uinfty is not None:
            Uinfty = np.array(Uinfty)
            if Uinfty.any():
                vel += Uinfty[:, np.newaxis]
        self._x_vortices += vel * dt

    def add_vortex(self, x, gamma):
        """Add a vortex to the list

        Parameters
        ----------
        x : 1d array
            Position of the new vortex
        gamma : float
            Strength of the new vortex
        """
        if self._x_vortices is None:
            self._x_vortices = x[:,np.newaxis]
        else:
            self._x_vortices = np.append(self._x_vortices,
                                         x[:,np.newaxis], axis=1)
        self._gamma.append(gamma)
        self._num_vortices += 1
        self._circulation += gamma

    def add_newly_shed(self, body):
        """Add a vortex newly shed from a body"""
        x_vort, gam = body.get_newly_shed()
        self.add_vortex(x_vort, gam)

    def induced_velocity(self, x):
        """Compute the induced velocity at the given point(s)"""
        vel = np.zeros_like(x)
        for xvort, gam in zip(np.transpose(self._x_vortices), self._gamma):
            vel += self.induced_velocity_single(x, xvort, gam)
        return vel

    @property
    def vortices(self):
        return self._x_vortices, self._gamma


class SourceDoubletPanels(object):
    def __init__(self, body):
        self._body = body
        self.panels = body.get_points()

    def update_positions(self):
        self.panels = self._body.get_points()

    def update_strengths(self, wake, Uinfty, dt):
        # compute influence coefficients and RHS and solve for strengths
        pass

    def get_wake_panel(self):
        return None

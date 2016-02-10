# -*- coding:utf-8 -*-

from __future__ import print_function, unicode_literals

import errno
import io
import logging
import platform
import signal
import subprocess
import sys
import tempfile
from itertools import chain

from _emerge.UserQuery import UserQuery

import portage
from portage import os
from portage import _encodings
from portage import _unicode_encode
from portage.output import (
	bold, create_color_func, green, red)
from portage.package.ebuild.digestgen import digestgen
from portage.process import find_binary, spawn
from portage.util import writemsg_level

from repoman.gpg import gpgsign, need_signature
from repoman import utilities
from repoman.modules.vcs.vcs import vcs_files_to_cps

bad = create_color_func("BAD")


class Actions(object):
	'''Handles post check result output and performs
	the various vcs activities for committing the results'''

	def __init__(self, repo_settings, options, scanner, vcs_settings):
		self.repo_settings = repo_settings
		self.options = options
		self.scanner = scanner
		self.vcs_settings = vcs_settings
		self.repoman_settings = repo_settings.repoman_settings
		self.suggest = {
			'ignore_masked': False,
			'include_dev': False,
		}
		if scanner.have['pmasked'] and not (options.without_mask or options.ignore_masked):
			self.suggest['ignore_masked'] = True
		if scanner.have['dev_keywords'] and not options.include_dev:
			self.suggest['include_dev'] = True


	def inform(self, can_force, result):
		'''Inform the user of all the problems found'''
		if ((self.suggest['ignore_masked'] or self.suggest['include_dev'])
				and not self.options.quiet):
			self._suggest()
		if self.options.mode != 'commit':
			self._non_commit(result)
			return False
		else:
			self._fail(result, can_force)
			if self.options.pretend:
				utilities.repoman_sez(
					"\"So, you want to play it safe. Good call.\"\n")
			return True


	def perform(self, qa_output):
		myautoadd = self._vcs_autoadd()

		self._vcs_deleted()

		changes = self.get_vcs_changed()

		mynew, mychanged, myremoved, no_expansion, expansion = changes

		# Manifests need to be regenerated after all other commits, so don't commit
		# them now even if they have changed.
		mymanifests = set()
		myupdates = set()
		for f in mychanged + mynew:
			if "Manifest" == os.path.basename(f):
				mymanifests.add(f)
			else:
				myupdates.add(f)
		myupdates.difference_update(myremoved)
		myupdates = list(myupdates)
		mymanifests = list(mymanifests)
		myheaders = []

		commitmessage = self.options.commitmsg
		if self.options.commitmsgfile:
			try:
				f = io.open(
					_unicode_encode(
						self.options.commitmsgfile,
						encoding=_encodings['fs'], errors='strict'),
					mode='r', encoding=_encodings['content'], errors='replace')
				commitmessage = f.read()
				f.close()
				del f
			except (IOError, OSError) as e:
				if e.errno == errno.ENOENT:
					portage.writemsg(
						"!!! File Not Found:"
						" --commitmsgfile='%s'\n" % self.options.commitmsgfile)
				else:
					raise
		if not commitmessage or not commitmessage.strip():
			commitmessage = self.get_new_commit_message(qa_output)

		commitmessage = commitmessage.rstrip()

		myupdates, broken_changelog_manifests = self.changelogs(
					myupdates, mymanifests, myremoved, mychanged, myautoadd,
					mynew, commitmessage)

		commit_footer = self.get_commit_footer()
		commitmessage += commit_footer

		print("* %s files being committed..." % green(str(len(myupdates))), end=' ')

		if not self.vcs_settings.needs_keyword_expansion:
			# With some VCS types there's never any keyword expansion, so
			# there's no need to regenerate manifests and all files will be
			# committed in one big commit at the end.
			logging.debug("VCS type doesn't need keyword expansion")
			print()
		elif not self.repo_settings.repo_config.thin_manifest:
			logging.debug("perform: Calling thick_manifest()")
			self.vcs_settings.changes.thick_manifest(myupdates, myheaders,
				no_expansion, expansion)

		logging.info("myupdates: %s", myupdates)
		logging.info("myheaders: %s", myheaders)

		uq = UserQuery(self.options)
		if self.options.ask and uq.query('Commit changes?', True) != 'Yes':
			print("* aborting commit.")
			sys.exit(128 + signal.SIGINT)

		# Handle the case where committed files have keywords which
		# will change and need a priming commit before the Manifest
		# can be committed.
		if (myupdates or myremoved) and myheaders:
			self.priming_commit(myupdates, myremoved, commitmessage)

		# When files are removed and re-added, the cvs server will put /Attic/
		# inside the $Header path. This code detects the problem and corrects it
		# so that the Manifest will generate correctly. See bug #169500.
		# Use binary mode in order to avoid potential character encoding issues.
		self.vcs_settings.changes.clear_attic(myheaders)

		if self.scanner.repolevel == 1:
			utilities.repoman_sez(
				"\"You're rather crazy... "
				"doing the entire repository.\"\n")

		self.vcs_settings.changes.digest_regen(myupdates, myremoved, mymanifests,
			self.scanner, broken_changelog_manifests)

		if self.repo_settings.sign_manifests:
			self.sign_manifest(myupdates, myremoved, mymanifests)

		self.vcs_settings.changes.update_index(mymanifests, myupdates)

		self.add_manifest(mymanifests, myheaders, myupdates, myremoved, commitmessage)

		if self.options.quiet:
			return
		print()
		if self.vcs_settings.vcs:
			print("Commit complete.")
		else:
			print(
				"repoman was too scared"
				" by not seeing any familiar version control file"
				" that he forgot to commit anything")
		utilities.repoman_sez(
			"\"If everyone were like you, I'd be out of business!\"\n")
		return


	def _suggest(self):
		print()
		if self.suggest['ignore_masked']:
			print(bold(
				"Note: use --without-mask to check "
				"KEYWORDS on dependencies of masked packages"))

		if self.suggest['include_dev']:
			print(bold(
				"Note: use --include-dev (-d) to check "
				"dependencies for 'dev' profiles"))
		print()


	def _non_commit(self, result):
		if result['full']:
			print(bold("Note: type \"repoman full\" for a complete listing."))
		if result['warn'] and not result['fail']:
			if self.options.quiet:
				print(bold("Non-Fatal QA errors found"))
			else:
				utilities.repoman_sez(
					"\"You're only giving me a partial QA payment?\n"
					"              I'll take it this time, but I'm not happy.\""
					)
		elif not result['fail']:
			if self.options.quiet:
				print("No QA issues found")
			else:
				utilities.repoman_sez(
					"\"If everyone were like you, I'd be out of business!\"")
		elif result['fail']:
			print(bad("Please fix these important QA issues first."))
			if not self.options.quiet:
				utilities.repoman_sez(
					"\"Make your QA payment on time"
					" and you'll never see the likes of me.\"\n")
			sys.exit(1)


	def _fail(self, result, can_force):
		if result['fail'] and can_force and self.options.force and not self.options.pretend:
			utilities.repoman_sez(
				" \"You want to commit even with these QA issues?\n"
				"              I'll take it this time, but I'm not happy.\"\n")
		elif result['fail']:
			if self.options.force and not can_force:
				print(bad(
					"The --force option has been disabled"
					" due to extraordinary issues."))
			print(bad("Please fix these important QA issues first."))
			utilities.repoman_sez(
				"\"Make your QA payment on time"
				" and you'll never see the likes of me.\"\n")
			sys.exit(1)


	def _vcs_autoadd(self):
		myunadded = self.vcs_settings.changes.unadded
		myautoadd = []
		if myunadded:
			for x in range(len(myunadded) - 1, -1, -1):
				xs = myunadded[x].split("/")
				if self.repo_settings.repo_config.find_invalid_path_char(myunadded[x]) != -1:
					# The Manifest excludes this file,
					# so it's safe to ignore.
					del myunadded[x]
				elif xs[-1] == "files":
					print("!!! files dir is not added! Please correct this.")
					sys.exit(-1)
				elif xs[-1] == "Manifest":
					# It's a manifest... auto add
					myautoadd += [myunadded[x]]
					del myunadded[x]

		if myunadded:
			print(red(
				"!!! The following files are in your local tree"
				" but are not added to the master"))
			print(red(
				"!!! tree. Please remove them from the local tree"
				" or add them to the master tree."))
			for x in myunadded:
				print("   ", x)
			print()
			print()
			sys.exit(1)
		return myautoadd


	def _vcs_deleted(self):
		if self.vcs_settings.changes.has_deleted:
			print(red(
				"!!! The following files are removed manually"
				" from your local tree but are not"))
			print(red(
				"!!! removed from the repository."
				" Please remove them, using \"%s remove [FILES]\"."
				% self.vcs_settings.vcs))
			for x in self.vcs_settings.changes.deleted:
				print("   ", x)
			print()
			print()
			sys.exit(1)


	def get_vcs_changed(self):
		'''Holding function which calls the approriate VCS module for the data'''
		changes = self.vcs_settings.changes
		# re-run the scan to pick up a newly modified Manifest file
		logging.debug("RE-scanning for changes...")
		changes.scan()

		if not changes.has_changes:
			utilities.repoman_sez(
				"\"Doing nothing is not always good for QA.\"")
			print()
			print("(Didn't find any changed files...)")
			print()
			sys.exit(1)
		return (changes.new, changes.changed, changes.removed,
				changes.no_expansion, changes.expansion)

	def get_commit_footer(self):
		portage_version = getattr(portage, "VERSION", None)
		gpg_key = self.repoman_settings.get("PORTAGE_GPG_KEY", "")
		dco_sob = self.repoman_settings.get("DCO_SIGNED_OFF_BY", "")
		report_options = []
		if self.options.force:
			report_options.append("--force")
		if self.options.ignore_arches:
			report_options.append("--ignore-arches")
		if self.scanner.include_arches is not None:
			report_options.append(
				"--include-arches=\"%s\"" %
				" ".join(sorted(self.scanner.include_arches)))

		if portage_version is None:
			sys.stderr.write("Failed to insert portage version in message!\n")
			sys.stderr.flush()
			portage_version = "Unknown"
		# Use new footer only for git (see bug #438364).
		if self.vcs_settings.vcs in ["git"]:
			commit_footer = "\n\nPackage-Manager: portage-%s" % portage_version
			if report_options:
				commit_footer += "\nRepoMan-Options: " + " ".join(report_options)
			if self.repo_settings.sign_manifests:
				commit_footer += "\nManifest-Sign-Key: %s" % (gpg_key, )
			if dco_sob:
				commit_footer += "\nSigned-off-by: %s" % (dco_sob, )
		else:
			unameout = platform.system() + " "
			if platform.system() in ["Darwin", "SunOS"]:
				unameout += platform.processor()
			else:
				unameout += platform.machine()
			commit_footer = "\n\n"
			if dco_sob:
				commit_footer += "Signed-off-by: %s\n" % (dco_sob, )
			commit_footer += "(Portage version: %s/%s/%s" % \
				(portage_version, self.vcs_settings.vcs, unameout)
			if report_options:
				commit_footer += ", RepoMan options: " + " ".join(report_options)
			if self.repo_settings.sign_manifests:
				commit_footer += ", signed Manifest commit with key %s" % \
					(gpg_key, )
			else:
				commit_footer += ", unsigned Manifest commit"
			commit_footer += ")"
		return commit_footer


	def changelogs(self, myupdates, mymanifests, myremoved, mychanged, myautoadd,
					mynew, changelog_msg):
		broken_changelog_manifests = []
		if self.options.echangelog in ('y', 'force'):
			logging.info("checking for unmodified ChangeLog files")
			committer_name = utilities.get_committer_name(env=self.repoman_settings)
			for x in sorted(vcs_files_to_cps(
				chain(myupdates, mymanifests, myremoved),
				self.scanner.repolevel, self.scanner.reposplit, self.scanner.categories)):
				catdir, pkgdir = x.split("/")
				checkdir = self.repo_settings.repodir + "/" + x
				checkdir_relative = ""
				if self.scanner.repolevel < 3:
					checkdir_relative = os.path.join(pkgdir, checkdir_relative)
				if self.scanner.repolevel < 2:
					checkdir_relative = os.path.join(catdir, checkdir_relative)
				checkdir_relative = os.path.join(".", checkdir_relative)

				changelog_path = os.path.join(checkdir_relative, "ChangeLog")
				changelog_modified = changelog_path in self.scanner.changed.changelogs
				if changelog_modified and self.options.echangelog != 'force':
					continue

				# get changes for this package
				cdrlen = len(checkdir_relative)
				check_relative = lambda e: e.startswith(checkdir_relative)
				split_relative = lambda e: e[cdrlen:]
				clnew = list(map(split_relative, filter(check_relative, mynew)))
				clremoved = list(map(split_relative, filter(check_relative, myremoved)))
				clchanged = list(map(split_relative, filter(check_relative, mychanged)))

				# Skip ChangeLog generation if only the Manifest was modified,
				# as discussed in bug #398009.
				nontrivial_cl_files = set()
				nontrivial_cl_files.update(clnew, clremoved, clchanged)
				nontrivial_cl_files.difference_update(['Manifest'])
				if not nontrivial_cl_files and self.options.echangelog != 'force':
					continue

				new_changelog = utilities.UpdateChangeLog(
					checkdir_relative, committer_name, changelog_msg,
					os.path.join(self.repo_settings.repodir, 'skel.ChangeLog'),
					catdir, pkgdir,
					new=clnew, removed=clremoved, changed=clchanged,
					pretend=self.options.pretend)
				if new_changelog is None:
					writemsg_level(
						"!!! Updating the ChangeLog failed\n",
						level=logging.ERROR, noiselevel=-1)
					sys.exit(1)

				# if the ChangeLog was just created, add it to vcs
				if new_changelog:
					myautoadd.append(changelog_path)
					# myautoadd is appended to myupdates below
				else:
					myupdates.append(changelog_path)

				if self.options.ask and not self.options.pretend:
					# regenerate Manifest for modified ChangeLog (bug #420735)
					self.repoman_settings["O"] = checkdir
					digestgen(mysettings=self.repoman_settings, myportdb=self.repo_settings.portdb)
				else:
					broken_changelog_manifests.append(x)

		if myautoadd:
			print(">>> Auto-Adding missing Manifest/ChangeLog file(s)...")
			add_cmd = [self.vcs_settings.vcs, "add"]
			add_cmd += myautoadd
			if self.options.pretend:
				portage.writemsg_stdout(
					"(%s)\n" % " ".join(add_cmd),
					noiselevel=-1)
			else:

				if sys.hexversion < 0x3020000 and sys.hexversion >= 0x3000000 and \
					not os.path.isabs(add_cmd[0]):
					# Python 3.1 _execvp throws TypeError for non-absolute executable
					# path passed as bytes (see http://bugs.python.org/issue8513).
					fullname = find_binary(add_cmd[0])
					if fullname is None:
						raise portage.exception.CommandNotFound(add_cmd[0])
					add_cmd[0] = fullname

				add_cmd = [_unicode_encode(arg) for arg in add_cmd]
				retcode = subprocess.call(add_cmd)
				if retcode != os.EX_OK:
					logging.error(
						"Exiting on %s error code: %s\n" % (self.vcs_settings.vcs, retcode))
					sys.exit(retcode)

			myupdates += myautoadd
		return myupdates, broken_changelog_manifests


	def add_manifest(self, mymanifests, myheaders, myupdates, myremoved,
					commitmessage):
		myfiles = mymanifests[:]
		# If there are no header (SVN/CVS keywords) changes in
		# the files, this Manifest commit must include the
		# other (yet uncommitted) files.
		if not myheaders:
			myfiles += myupdates
			myfiles += myremoved
		myfiles.sort()

		fd, commitmessagefile = tempfile.mkstemp(".repoman.msg")
		mymsg = os.fdopen(fd, "wb")
		mymsg.write(_unicode_encode(commitmessage))
		mymsg.close()

		commit_cmd = []
		if self.options.pretend and self.vcs_settings.vcs is None:
			# substitute a bogus value for pretend output
			commit_cmd.append("cvs")
		else:
			commit_cmd.append(self.vcs_settings.vcs)
		commit_cmd.extend(self.vcs_settings.vcs_global_opts)
		commit_cmd.append("commit")
		commit_cmd.extend(self.vcs_settings.vcs_local_opts)
		if self.vcs_settings.vcs == "hg":
			commit_cmd.extend(["--logfile", commitmessagefile])
			commit_cmd.extend(myfiles)
		else:
			commit_cmd.extend(["-F", commitmessagefile])
			commit_cmd.extend(f.lstrip("./") for f in myfiles)

		try:
			if self.options.pretend:
				print("(%s)" % (" ".join(commit_cmd),))
			else:
				retval = spawn(commit_cmd, env=self.repo_settings.commit_env)
				if retval != os.EX_OK:
					if self.repo_settings.repo_config.sign_commit and not self.vcs_settings.status.supports_gpg_sign():
						# Inform user that newer git is needed (bug #403323).
						logging.error(
							"Git >=1.7.9 is required for signed commits!")

					writemsg_level(
						"!!! Exiting on %s (shell) "
						"error code: %s\n" % (self.vcs_settings.vcs, retval),
						level=logging.ERROR, noiselevel=-1)
					sys.exit(retval)
		finally:
			try:
				os.unlink(commitmessagefile)
			except OSError:
				pass


	def priming_commit(self, myupdates, myremoved, commitmessage):
		myfiles = myupdates + myremoved
		fd, commitmessagefile = tempfile.mkstemp(".repoman.msg")
		mymsg = os.fdopen(fd, "wb")
		mymsg.write(_unicode_encode(commitmessage))
		mymsg.close()

		separator = '-' * 78

		print()
		print(green("Using commit message:"))
		print(green(separator))
		print(commitmessage)
		print(green(separator))
		print()

		# Having a leading ./ prefix on file paths can trigger a bug in
		# the cvs server when committing files to multiple directories,
		# so strip the prefix.
		myfiles = [f.lstrip("./") for f in myfiles]

		commit_cmd = [self.vcs_settings.vcs]
		commit_cmd.extend(self.vcs_settings.vcs_global_opts)
		commit_cmd.append("commit")
		commit_cmd.extend(self.vcs_settings.vcs_local_opts)
		commit_cmd.extend(["-F", commitmessagefile])
		commit_cmd.extend(myfiles)

		try:
			if self.options.pretend:
				print("(%s)" % (" ".join(commit_cmd),))
			else:
				retval = spawn(commit_cmd, env=self.repo_settings.commit_env)
				if retval != os.EX_OK:
					writemsg_level(
						"!!! Exiting on %s (shell) "
						"error code: %s\n" % (self.vcs_settings.vcs, retval),
						level=logging.ERROR, noiselevel=-1)
					sys.exit(retval)
		finally:
			try:
				os.unlink(commitmessagefile)
			except OSError:
				pass


	def sign_manifest(self, myupdates, myremoved, mymanifests):
		try:
			for x in sorted(vcs_files_to_cps(
				chain(myupdates, myremoved, mymanifests),
				self.scanner.repolevel, self.scanner.reposplit, self.scanner.categories)):
				self.repoman_settings["O"] = os.path.join(self.repo_settings.repodir, x)
				manifest_path = os.path.join(self.repoman_settings["O"], "Manifest")
				if not need_signature(manifest_path):
					continue
				gpgsign(manifest_path, self.repoman_settings, self.options)
		except portage.exception.PortageException as e:
			portage.writemsg("!!! %s\n" % str(e))
			portage.writemsg("!!! Disabled FEATURES='sign'\n")
			self.repo_settings.sign_manifests = False


	def get_new_commit_message(self, qa_output):
		msg_prefix = ""
		if self.scanner.repolevel > 1:
			msg_prefix = "/".join(self.scanner.reposplit[1:]) + ": "

		try:
			editor = os.environ.get("EDITOR")
			if editor and utilities.editor_is_executable(editor):
				commitmessage = utilities.get_commit_message_with_editor(
					editor, message=qa_output, prefix=msg_prefix)
			else:
				commitmessage = utilities.get_commit_message_with_stdin()
		except KeyboardInterrupt:
			logging.fatal("Interrupted; exiting...")
			sys.exit(1)
		if (not commitmessage or not commitmessage.strip()
				or commitmessage.strip() == msg_prefix):
			print("* no commit message?  aborting commit.")
			sys.exit(1)
		return commitmessage
